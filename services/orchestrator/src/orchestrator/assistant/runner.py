"""One chat turn: store inputs -> run the assistant (or a direct screening) -> store the answer -> stream events.

Abort: `POST .../turns/{turnId}/abort` (or the client disconnecting) cancels the turn task. The task then cancels
every in-flight agent request (the agent stops its LLM call), stores a "stopped" message and emits `aborted`.
"""
import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.errors import GraphRecursionError

from ats_common.context import bind
from ats_common.db.repositories import TenantScope
from ats_common.errors import AppError, Errors, error_body
from ats_common.logging import get_logger, log_event
from ats_common.security import UserClaims
from orchestrator import metrics
from orchestrator.api.v1.schemas import MessageOut
from orchestrator.assistant.graph import RECURSION_LIMIT, build_assistant_graph
from orchestrator.assistant.prompts import chat_context
from orchestrator.assistant.tools import TurnContext, build_tools, perform_screening, summarize
from orchestrator.assistant.tracker import TurnTracker, sse
from orchestrator.constants import DEFAULT_CHAT_TITLE, GAP_GRADES, MessageRole, TurnAction, TurnEvent
from orchestrator.container import get_container
from orchestrator.errors import OrchErrors
from orchestrator.models import DocumentKind
from orchestrator.repositories import ChatRepository, DocumentRepository, ScreeningRepository
from orchestrator.services import documents
from orchestrator.services.documents import UploadedFile
from orchestrator.services.turn_registry import RunningTurn, TurnRegistry

log = get_logger(__name__)
TERMINAL_EVENTS = {TurnEvent.MESSAGE, TurnEvent.ABORTED, TurnEvent.ERROR}
AGENT_CANCEL_TIMEOUT_S = 10


@dataclass
class TurnInput:
    user: UserClaims
    scope: TenantScope
    chat_id: str
    content: str
    action: TurnAction
    job_description: Optional[str] = None     # replaces the chat's JD text when given
    resume_upload: Optional[UploadedFile] = None
    jd_upload: Optional[UploadedFile] = None  # JD as a file (PDF/TXT/MD) instead of text


def _message_out(message) -> dict:
    return MessageOut.model_validate(message).model_dump(mode="json", by_alias=True)


def _history(messages) -> list[BaseMessage]:
    return [HumanMessage(m.content) if m.role == MessageRole.USER else AIMessage(m.content)
            for m in messages if m.role in (MessageRole.USER, MessageRole.ASSISTANT)]


def _screening_reply(result: dict) -> str:
    gaps = [r["name"] for r in result.get("requirements", []) if r["grade"] in GAP_GRADES]
    return (f"**{result['score']}/100 - {result['band']}** · {result['recommendation']}\n\n"
            f"{result.get('summary', '')}\n\n"
            + (f"**Gaps:** {', '.join(gaps)}\n\n" if gaps else "")
            + "Ask me about any requirement, the gaps, or the interview questions.")


async def _store_inputs(turn: TurnInput, tracker: TurnTracker) -> None:
    """Store uploaded resume / JD (documents + hybrid-search index), update the chat, record the user message."""
    attachments: list[dict] = []
    resume_doc = jd_doc = None
    if turn.resume_upload:
        tracker.emit("pdf", f"Reading resume {turn.resume_upload.filename}...", 5)
        resume_doc, created = await documents.ingest(turn.scope, turn.user.user_id, DocumentKind.RESUME,
                                                     turn.resume_upload)
        attachments.append({"kind": "resume", "filename": resume_doc.filename, "pages": resume_doc.pages,
                            "characters": len(resume_doc.text)})
        state = "indexed" if created else "already indexed"
        tracker.emit("index", f"Resume ready ({resume_doc.pages} page(s), {state})", 12)
    if turn.jd_upload:
        tracker.emit("pdf", f"Reading job description {turn.jd_upload.filename}...", 13)
        jd_doc, _ = await documents.ingest(turn.scope, turn.user.user_id, DocumentKind.JD, turn.jd_upload)
        attachments.append({"kind": "jd", "filename": jd_doc.filename, "pages": jd_doc.pages,
                            "characters": len(jd_doc.text)})
        tracker.emit("index", f"Job description ready ({jd_doc.pages} page(s))", 16)
    elif turn.job_description and turn.job_description.strip():
        jd_doc = await documents.ingest_text(turn.scope, turn.user.user_id, DocumentKind.JD,
                                             turn.job_description.strip())

    async with get_container().db.session() as session:
        chats = ChatRepository(session, turn.scope)
        chat = await chats.get_owned(turn.chat_id, turn.user.user_id)
        if resume_doc:
            chat.resume_document_id = resume_doc.id
        if jd_doc:
            chat.jd_document_id, chat.jd_text = jd_doc.id, jd_doc.text
        elif turn.job_description is not None:
            chat.jd_text, chat.jd_document_id = None, None
        if chat.title == DEFAULT_CHAT_TITLE and turn.action == TurnAction.CHAT:
            chat.title = turn.content[:60]
        await chats.touch(chat)
        user_msg = await chats.add_message(turn.chat_id, MessageRole.USER, turn.content, {
            "action": turn.action.value, "attachments": attachments,
            "jdUpdated": bool(jd_doc) or turn.job_description is not None})
        tracker.push({"type": TurnEvent.USER_MESSAGE, "message": _message_out(user_msg)})


async def _answer(turn: TurnInput, ctx: TurnContext) -> str:
    if turn.action == TurnAction.SCREEN:  # explicit "Screen candidate": no LLM decision needed
        ctx.tools_used.append("screen_candidate")
        return _screening_reply(await perform_screening(ctx))

    c = get_container()
    async with c.db.session() as session:
        chat = await ChatRepository(session, turn.scope).get_owned(turn.chat_id, turn.user.user_id)
        resume = await DocumentRepository(session, turn.scope).get_scoped(chat.resume_document_id) \
            if chat.resume_document_id else None
        latest = await ScreeningRepository(session, turn.scope).latest_for_chat(turn.chat_id)
        history = await ChatRepository(session, turn.scope).list_messages(turn.chat_id, limit=c.settings.history_messages)

    context = chat_context(chat.jd_text, resume.filename if resume else None, latest.result if latest else None)
    graph = build_assistant_graph(c.settings, build_tools(ctx), context, ctx.budget)
    ctx.tracker.emit("thinking", "Assistant is deciding what to do...", 20)
    final, tool_outputs = "", []
    try:
        async for update in graph.astream({"messages": _history(history)}, {"recursion_limit": RECURSION_LIMIT},
                                          stream_mode="updates"):
            for node, payload in update.items():
                for msg in payload.get("messages", []):
                    if node == "assistant" and getattr(msg, "tool_calls", None):
                        names = ", ".join(call["name"] for call in msg.tool_calls)
                        ctx.tracker.emit("tool_call", f"Assistant decided to call: {names}", 22, tools=names)
                    elif node == "tools":
                        tool_outputs.append(str(msg.content))
                        ctx.tracker.emit("responding", "Assistant is writing the answer...", 92)
                    elif node == "assistant" and isinstance(msg, AIMessage):
                        final = msg.content if isinstance(msg.content, str) else str(msg.content)
    except GraphRecursionError:
        log_event(log, "assistant.loop_limit", tools=ctx.tools_used)
    if final.strip():
        return final.strip()
    if ctx.screening:
        return summarize(ctx.screening)
    useful = [o for o in tool_outputs if not o.startswith("Already called")]
    return ("Here is what I found:\n\n" + useful[-1][:3000]) if useful else \
        "I could not produce an answer. Please rephrase the question or press Screen candidate."


async def _cancel_agent_requests(running: RunningTurn) -> None:
    c = get_container()
    for request_id in list(running.agent_request_ids):
        await asyncio.wait_for(c.agent.cancel(request_id), AGENT_CANCEL_TIMEOUT_S)


async def _save_assistant(turn: TurnInput, content: str, data: dict) -> dict:
    async with get_container().db.session() as session:
        chats = ChatRepository(session, turn.scope)
        message = await chats.add_message(turn.chat_id, MessageRole.ASSISTANT, content, data)
        chat = await chats.get_owned(turn.chat_id, turn.user.user_id)
        await chats.touch(chat)
        return {"message": _message_out(message), "chatTitle": chat.title}


async def _run_turn(turn: TurnInput, tracker: TurnTracker, running: RunningTurn) -> None:
    """Always ends with exactly one message | aborted | error event."""
    c = get_container()
    labels = {"client_id": str(turn.scope.client_id), "project_id": str(turn.scope.project_id),
              "action": turn.action.value}
    started, outcome = time.perf_counter(), "ok"
    metrics.active_turns.add(1, {"client_id": labels["client_id"]})
    log_event(log, "turn.started", action=turn.action.value, has_resume_file=bool(turn.resume_upload),
              has_jd_file=bool(turn.jd_upload))
    ctx = TurnContext(user=turn.user, scope=turn.scope, chat_id=turn.chat_id, tracker=tracker, running=running)
    try:
        await _store_inputs(turn, tracker)

        def on_position(position: int) -> None:
            tracker.emit("queued", f"The model is busy. You are number {position} in the queue...", 19,
                         queue_position=position)

        error_code = None
        try:
            async with c.queue.slot(on_position):
                answer = await _answer(turn, ctx)
        except AppError as e:  # expected, user-actionable (missing JD/resume, agent unavailable, ...)
            outcome, error_code = "rejected", e.spec.code
            answer = f"{e.spec.message} {e.spec.hint}"
        saved = await _save_assistant(turn, answer, {"screening": ctx.screening, "screeningId": ctx.screening_id,
                                                     "toolsUsed": ctx.tools_used, "errorCode": error_code})
        tracker.push({"type": TurnEvent.MESSAGE, "progress": 100, **saved})
    except asyncio.CancelledError:
        outcome = "aborted"
        metrics.aborts_total.add(1, {"client_id": labels["client_id"]})
        try:
            await _cancel_agent_requests(running)
            saved = await _save_assistant(turn, "Stopped. No further work was done on this request.",
                                          {"aborted": True, "toolsUsed": ctx.tools_used,
                                           "errorCode": OrchErrors.TURN_ABORTED.code})
            tracker.push({"type": TurnEvent.ABORTED, **saved})
        except Exception:
            log.exception("turn.abort_cleanup_failed")
        raise
    except AppError as e:
        outcome = "error"
        tracker.push({"type": TurnEvent.ERROR, **error_body(e.spec, retry_after=e.retry_after)})
    except Exception:
        outcome = "error"
        log.exception("turn.failed")
        tracker.push({"type": TurnEvent.ERROR, **error_body(Errors.INTERNAL)})
    finally:
        duration = time.perf_counter() - started
        metrics.active_turns.add(-1, {"client_id": labels["client_id"]})
        metrics.turns_total.add(1, {**labels, "outcome": outcome})
        metrics.turn_duration.record(duration, {"action": labels["action"], "outcome": outcome})
        log_event(log, "turn.completed", action=turn.action.value, outcome=outcome, duration_s=round(duration, 1),
                  tools=ctx.tools_used)


async def start_turn(turn: TurnInput) -> AsyncIterator[str]:
    """SSE generator: starts the turn task, forwards its events, sends heartbeats, and cancels the task if the
    client disconnects or an abort is requested (also from another replica, via the Redis flag)."""
    c = get_container()
    turn_id = TurnRegistry.new_turn_id()
    bind(chat_id=turn.chat_id, turn_id=turn_id)
    tracker = TurnTracker(turn_id)
    running = RunningTurn(turn_id, turn.chat_id, turn.user.user_id, turn.scope.client_id, turn.scope.project_id)
    c.turns.start(running)
    tracker.push({"type": TurnEvent.TURN_STARTED, "chatId": turn.chat_id})
    running.task = asyncio.create_task(_run_turn(turn, tracker, running))
    try:
        while True:
            try:
                event = await asyncio.wait_for(tracker.queue.get(), timeout=c.settings.heartbeat_seconds)
            except asyncio.TimeoutError:
                if running.task.done() and tracker.queue.empty():
                    break
                if not running.aborted and await c.turns.is_aborted(turn_id):
                    await c.turns.abort(running)
                yield sse(tracker.heartbeat())
                continue
            yield sse(event)
            if event.get("type") in TERMINAL_EVENTS:
                break
    finally:
        if not running.task.done():
            running.task.cancel()        # client went away: stop paying for generation
        c.turns.finish(turn_id)
