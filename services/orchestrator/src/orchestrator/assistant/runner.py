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
from ats_common.llm import Purpose, model_for
from ats_common.prompt_safety import detect
from ats_common.security import UserClaims
from orchestrator import metrics
from orchestrator.api.v1.schemas import MessageOut
from orchestrator.assistant.graph import RECURSION_LIMIT, build_assistant_graph
from orchestrator.assistant.guards import guard_scores
from orchestrator.assistant.journeys import journey_for
from orchestrator.assistant.memory import memory_prompt, remember_turn
from orchestrator.assistant.prompts import SECURITY_NOTE, chat_context, grounded_question
from orchestrator.assistant.tools import TurnContext, build_tools, perform_screening, summarize
from orchestrator.assistant.tracker import TurnTracker, sse
from orchestrator.constants import DEFAULT_CHAT_TITLE, GAP_GRADES, MessageRole, TurnAction, TurnEvent
from orchestrator.container import get_container
from orchestrator.errors import OrchErrors
from orchestrator.models import DocumentKind
from orchestrator.models import ChatTurn
from orchestrator.repositories import (ChatMemoryRepository, ChatRepository, ChatTurnRepository, DocumentRepository,
                                       ScreeningRepository)
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
        knowledge = get_container().knowledge
        if knowledge.enabled:   # background: builds the candidate graph while screening and chat go on
            knowledge.remember_profile(turn.scope, resume_doc.id, resume_doc.text)
            tracker.emit("knowledge", "Adding the profile to the candidate knowledge graph (background)", 13)
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


def with_excerpts(messages: list[BaseMessage], excerpts: str) -> list[BaseMessage]:
    """Attach the excerpts to the latest user message (the current question) for the prompt only."""
    if not excerpts or not messages or not isinstance(messages[-1], HumanMessage):
        return messages
    return [*messages[:-1], HumanMessage(grounded_question(str(messages[-1].content), excerpts))]


async def _ground(turn: TurnInput, chat, ctx: TurnContext) -> str:
    """Excerpts of the chat's resume/JD for the user's question (headers pinned), for the assistant's context.

    Retrieval runs on every chat turn so a small model that skips tools still answers from the documents.
    In parallel, the knowledge graph gives this chat's candidate's typed attributes and facts (skipped when it has
    nothing yet or is slow). Returns "" when the chat has no documents.
    """
    ids = documents.chat_document_ids(chat)
    if not ids:
        return ""
    c = get_container()
    question = turn.content[:500]
    ctx.tracker.emit("grounding", "Reading the relevant parts of the resume, JD and knowledge graph...", 18)
    graph_lookup = (c.knowledge.candidate_facts(turn.scope, chat.resume_document_id, question)
                    if chat.resume_document_id else _no_facts())
    hits, graph_facts = await asyncio.gather(
        documents.search(turn.scope, ids, question, c.settings.grounding_chunks), graph_lookup)
    log_event(log, "assistant.grounded", chunks=[f"{h.kind}#{h.chunk_index}" for h in hits],
              graph_lines=graph_facts.count("\n") + 1 if graph_facts else 0)
    excerpts = documents.format_hits(hits, c.settings.grounding_max_chars)
    ctx.record["grounding"] = {"chunks": [f"{h.kind}#{h.chunk_index}" for h in hits],
                               "graphLines": graph_facts.count("\n") + 1 if graph_facts else 0}
    return f"{excerpts}\n\n{GRAPH_EXCERPT_LABEL}\n{graph_facts}" if graph_facts else excerpts


GRAPH_EXCERPT_LABEL = "[knowledge graph: this candidate]"


async def _no_facts() -> str:
    return ""


async def _answer(turn: TurnInput, ctx: TurnContext) -> str:
    """Run the turn's journey (journeys.py) and return the reply text."""
    journey = journey_for(turn.action)
    if not journey.chat_model:  # "screen": the agent's skill decides, the reply is built in code
        ctx.tools_used.append("screen_candidate")
        return _screening_reply(await perform_screening(ctx))

    c = get_container()
    async with c.db.session() as session:
        chat = await ChatRepository(session, turn.scope).get_owned(turn.chat_id, turn.user.user_id)
        resume = await DocumentRepository(session, turn.scope).get_scoped(chat.resume_document_id) \
            if chat.resume_document_id else None
        latest = await ScreeningRepository(session, turn.scope).latest_for_chat(turn.chat_id)
        history = await ChatRepository(session, turn.scope).list_messages(turn.chat_id, limit=c.settings.history_messages)
        memory = await ChatMemoryRepository(session, turn.scope).for_chat(turn.chat_id) if journey.memory else None

    context = chat_context(chat.jd_text, resume.filename if resume else None, latest.result if latest else None)
    context += _security_note(turn.content, ctx)
    remembered = memory_prompt(memory)
    context += f"\n\n{remembered}" if remembered else ""
    excerpts = await _ground(turn, chat, ctx) if journey.grounding else ""
    messages = with_excerpts(_history(history), excerpts)
    ctx.record.update(model=model_for(Purpose.CHAT, turn.scope.client_id), historyMessages=len(history),
                      memory={"used": bool(remembered), "version": memory.version if memory else None})
    graph = build_assistant_graph(build_tools(ctx), context, ctx.budget)
    ctx.tracker.emit("thinking", "Assistant is deciding what to do...", 20)
    final, tool_outputs = "", []
    try:
        async for update in graph.astream({"messages": messages}, {"recursion_limit": RECURSION_LIMIT},
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
    guarded = guard_scores(_reply(final, ctx, tool_outputs), ctx.screening or (latest.result if latest else None))
    if guarded.corrected:
        metrics.score_corrections.add(1, {"client_id": str(turn.scope.client_id)})
        log_event(log, "assistant.score_corrected", claimed=list(guarded.claimed))
        ctx.record["scoreCorrected"] = list(guarded.claimed)
    return guarded.reply


def _reply(final: str, ctx: TurnContext, tool_outputs: list[str]) -> str:
    """The model's answer; without one, the screening summary or the last useful tool result."""
    if final.strip():
        return final.strip()
    if ctx.screening:
        return summarize(ctx.screening)
    useful = [o for o in tool_outputs if not o.startswith("Already called")]
    return ("Here is what I found:\n\n" + useful[-1][:3000]) if useful else \
        "I could not produce an answer. Please rephrase the question or press Screen candidate."


def _security_note(message: str, ctx: TurnContext) -> str:
    """A note for the model when the user's message looks like an injection (logged, counted, recorded in the
    turn record; never dropped)."""
    report = detect(message)
    if not report.suspicious:
        return ""
    ctx.record["injection"] = list(report.kinds)
    metrics.injection_flags.add(1, {"source": "chat_message"})
    log_event(log, "security.injection_detected", source="chat_message", kinds=list(report.kinds))
    return "\n\n" + SECURITY_NOTE.format(kinds=", ".join(report.kinds))


async def _cancel_agent_requests(running: RunningTurn) -> None:
    c = get_container()
    for request_id in list(running.agent_request_ids):
        await asyncio.wait_for(c.agent.cancel(request_id), AGENT_CANCEL_TIMEOUT_S)
    for request_id in list(running.knowledge_request_ids):
        await asyncio.wait_for(c.knowledge.cancel(request_id), AGENT_CANCEL_TIMEOUT_S)


async def _save_assistant(turn: TurnInput, content: str, data: dict) -> dict:
    async with get_container().db.session() as session:
        chats = ChatRepository(session, turn.scope)
        message = await chats.add_message(turn.chat_id, MessageRole.ASSISTANT, content, data)
        chat = await chats.get_owned(turn.chat_id, turn.user.user_id)
        await chats.touch(chat)
        return {"message": _message_out(message), "chatTitle": chat.title}


def turn_record(turn: TurnInput, ctx: TurnContext, outcome: str, error_code: Optional[str],
                duration_s: float) -> ChatTurn:
    """The JSON record of one turn: journey + skill, steps with timings, tools, grounding, memory, security events,
    model alias and outcome. Ids and labels only: message text lives in chat_messages, never here."""
    record = {**ctx.record, "action": turn.action.value, "tools": ctx.tools_used, "steps": ctx.tracker.steps,
              "screeningId": ctx.screening_id, "uploads": {"resume": bool(turn.resume_upload),
                                                           "jd": bool(turn.jd_upload)}}
    return ChatTurn(id=ctx.tracker.turn_id, client_id=turn.scope.client_id, project_id=turn.scope.project_id,
                    chat_id=turn.chat_id, user_id=turn.user.user_id, journey=ctx.record.get("journey", "unknown"),
                    outcome=outcome, error_code=error_code, duration_ms=round(duration_s * 1000), record=record)


async def _save_turn_record(turn: TurnInput, ctx: TurnContext, outcome: str, error_code: Optional[str],
                            duration_s: float) -> None:
    """Store the turn record; a failure is logged and never affects the turn."""
    try:
        async with get_container().db.session() as session:
            await ChatTurnRepository(session, turn.scope).add(turn_record(turn, ctx, outcome, error_code, duration_s))
    except Exception as e:
        log_event(log, "turn.record_failed", error_type=type(e).__name__)


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
    journey = journey_for(turn.action)
    ctx.record.update(journey=journey.name, skill=journey.skill)
    error_code = None
    try:
        await _store_inputs(turn, tracker)

        def on_position(position: int) -> None:
            tracker.emit("queued", f"The model is busy. You are number {position} in the queue...", 19,
                         queue_position=position)

        try:
            async with c.queue.slot(on_position):
                answer = await _answer(turn, ctx)
        except AppError as e:  # expected, user-actionable (missing JD/resume, agent unavailable, ...)
            outcome, error_code = "rejected", e.spec.code
            answer = f"{e.spec.message} {e.spec.hint}"
        saved = await _save_assistant(turn, answer, {"screening": ctx.screening, "screeningId": ctx.screening_id,
                                                     "toolsUsed": ctx.tools_used, "errorCode": error_code})
        tracker.push({"type": TurnEvent.MESSAGE, "progress": 100, **saved})
        if journey.memory and outcome == "ok":
            remember_turn(turn.scope, turn.chat_id)       # background; the reply is already sent
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
        outcome, error_code = "error", e.spec.code
        tracker.push({"type": TurnEvent.ERROR, **error_body(e.spec, retry_after=e.retry_after)})
    except Exception:
        outcome, error_code = "error", Errors.INTERNAL.code
        log.exception("turn.failed")
        tracker.push({"type": TurnEvent.ERROR, **error_body(Errors.INTERNAL)})
    finally:
        duration = time.perf_counter() - started
        await asyncio.shield(_save_turn_record(turn, ctx, outcome, error_code, duration))
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
