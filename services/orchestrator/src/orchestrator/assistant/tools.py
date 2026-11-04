"""Tools of the conversational assistant, bound to one chat turn (user, tenant, chat, tracker, abort registry)."""
import json
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional

from langchain_core.tools import BaseTool, tool

from ats_common.db.repositories import TenantScope
from ats_common.errors import AppError
from ats_common.logging import get_logger, log_event
from ats_common.redis_keys import Keys, build_key, digest, ttl
from ats_common.security import UserClaims
from orchestrator import metrics
from orchestrator.assistant.tracker import TurnTracker
from orchestrator.constants import DEFAULT_CHAT_TITLE, GAP_GRADES, MET_GRADES
from orchestrator.container import get_container
from orchestrator.errors import OrchErrors
from orchestrator.models import Screening
from orchestrator.models import DocumentKind
from orchestrator.repositories import ChatRepository, DocumentRepository, ScreeningRepository
from orchestrator.services import documents
from orchestrator.assistant.calculator import calculate as run_calculation
from orchestrator.services.agent_client import AgentTenant
from orchestrator.services.step_budget import StepBudget
from orchestrator.services.turn_registry import RunningTurn

log = get_logger(__name__)
AGENT_STEP_PROGRESS = {"agent": 30, "analyze_job_description": 40, "analyze_candidate_profile": 55,
                       "match_requirements": 70, "write_verdict": 82, "done": 90}


@dataclass
class TurnContext:
    user: UserClaims
    scope: TenantScope
    chat_id: str
    tracker: TurnTracker
    running: RunningTurn
    screening: Optional[dict] = None
    screening_id: Optional[int] = None
    tools_used: list[str] = field(default_factory=list)
    budget: StepBudget = field(default_factory=lambda: StepBudget(get_container().settings.max_steps_per_request))

    @property
    def agent_tenant(self) -> AgentTenant:
        return AgentTenant(self.scope.client_id, self.scope.project_id, self.user.user_id)


def summarize(result: dict) -> str:
    """Compact screening summary for the LLM (the full result goes to the UI)."""
    reqs = result.get("requirements", [])
    met = [r["name"] for r in reqs if r["grade"] in MET_GRADES]
    gaps = [f"{r['name']} ({r['importance']})" for r in reqs if r["grade"] in GAP_GRADES]
    return (f"Screening done. Score {result['score']}/100 ({result['band']}), recommendation: "
            f"{result['recommendation']}. Role: {result.get('roleTitle') or 'n/a'}. "
            f"Met: {', '.join(met) or 'none'}. Gaps: {', '.join(gaps) or 'none'}. "
            f"Summary: {result.get('summary', '')[:600]}")


async def perform_screening(ctx: TurnContext) -> dict:
    """Run (or reuse) a screening of the chat's resume against its JD, store it, and return the result."""
    c = get_container()
    async with c.db.session() as session:
        chat = await ChatRepository(session, ctx.scope).get_owned(ctx.chat_id, ctx.user.user_id)
        resume = await DocumentRepository(session, ctx.scope).get_scoped(chat.resume_document_id) \
            if chat and chat.resume_document_id else None
        jd_text = chat.jd_text if chat else None
    if not jd_text:
        raise AppError(OrchErrors.JD_MISSING)
    if not resume:
        raise AppError(OrchErrors.RESUME_MISSING)

    jd_hash = digest(jd_text)
    cache_key = build_key(Keys.SCREENING_RESULT, c.settings.app_env, client_id=ctx.scope.client_id,
                          project_id=ctx.scope.project_id,
                          digest=digest(c.settings.skill_version or "default", jd_hash, resume.text_hash))
    cached = await c.redis.get(cache_key)
    if cached:
        result = json.loads(cached)
        ctx.tracker.emit("agent_cache", "Same resume and JD were screened before - reusing that result", 88)
    else:
        async def progress(step: str, message: str, _: float) -> None:
            ctx.tracker.emit(f"agent:{step}", message, AGENT_STEP_PROGRESS.get(step))

        request_id = uuid.uuid4().hex
        ctx.running.agent_request_ids.add(request_id)
        ctx.tracker.emit("agent:start", "Sending the resume and JD to the screening agent (MCP)...", 25)
        # On cancellation the id stays registered so the abort handler can tell the agent to stop its LLM call.
        result = await c.agent.screen(ctx.agent_tenant, request_id, ctx.chat_id, jd_text, resume.text, progress)
        ctx.running.agent_request_ids.discard(request_id)
        await c.redis.setex(cache_key, ttl(Keys.SCREENING_RESULT), json.dumps(result))

    async with c.db.session() as session:
        row = await ScreeningRepository(session, ctx.scope).add(Screening(
            client_id=ctx.scope.client_id, project_id=ctx.scope.project_id, chat_id=ctx.chat_id,
            user_id=ctx.user.user_id, resume_document_id=resume.id, jd_hash=jd_hash, score=result["score"],
            band=result["band"], recommendation=result["recommendation"], skill=result.get("skill", "")[:128],
            result=result))
        chats = ChatRepository(session, ctx.scope)
        chat = await chats.get_owned(ctx.chat_id, ctx.user.user_id)
        chat.last_score = result["score"]
        if chat.title == DEFAULT_CHAT_TITLE and result.get("roleTitle"):
            chat.title = f"{result['roleTitle']} - {resume.filename}"[:200]
        await chats.touch(chat)
        ctx.screening_id = row.id

    labels = {"client_id": str(ctx.scope.client_id), "project_id": str(ctx.scope.project_id)}
    metrics.screenings_total.add(1, {**labels, "recommendation": result["recommendation"]})
    metrics.screening_score.record(result["score"], labels)
    log_event(log, "screening.completed", chat_id=ctx.chat_id, score=result["score"],
              recommendation=result["recommendation"], cached=bool(cached))
    ctx.screening = result
    return result


def build_tools(ctx: TurnContext) -> list[BaseTool]:
    @tool
    async def screen_candidate() -> str:
        """Screen the chat's resume against the chat's job description and return the result summary."""
        ctx.tools_used.append("screen_candidate")
        try:
            return summarize(await perform_screening(ctx))
        except AppError as e:
            return f"Cannot screen: {e.spec.message} {e.spec.hint}"

    @tool
    async def get_screening_details(
        section: Literal["requirements", "gaps", "questions", "summary", "breakdown"] = "requirements",
    ) -> str:
        """Read one section of this chat's latest screening.

        Args:
            section: requirements (every requirement with grade and evidence), gaps, questions (interview
                questions with expected answers), summary, or breakdown (how the score was computed).
        """
        ctx.tools_used.append(f"get_screening_details:{section}")
        ctx.tracker.emit("tool:details", f"Reading the latest screening ({section})...", 60)
        async with get_container().db.session() as session:
            latest = await ScreeningRepository(session, ctx.scope).latest_for_chat(ctx.chat_id)
        if not latest:
            return "No screening in this chat yet."
        r = latest.result
        reqs = r.get("requirements", [])
        if section == "requirements":
            return "\n".join(f"- {q['name']} [{q['importance']}] {q['grade']}: {q.get('evidence') or '-'}" for q in reqs)
        if section == "gaps":
            return "\n".join(f"- {q['name']} [{q['importance']}] {q['grade']}; JD says: \"{q['jdQuote']}\""
                             + (f"; closest: {', '.join(q['matchedSkills'])}" if q.get("matchedSkills") else "")
                             for q in reqs if q["grade"] in GAP_GRADES) or "No gaps."
        if section == "questions":
            return "\n".join(f"Q{i}: [{q['skill']}] {q['question']}\nExpected: {q['expectedAnswer']} "
                             f"Red flag: {q.get('redFlag', '')}" for i, q in enumerate(r.get("questions", []), 1))
        if section == "breakdown":
            return json.dumps({"score": r["score"], **r.get("breakdown", {})})
        return f"{r.get('summary', '')}\nRecommendation: {r['recommendation']}. {r.get('recommendationReason', '')}"

    @tool
    async def ask_screening_agent(question: str) -> str:
        """Ask the screening agent, which remembers this recruiter's earlier screenings in this project.

        Args:
            question: The question, e.g. "Which earlier candidates scored above 70?"
        """
        ctx.tools_used.append("ask_screening_agent")
        ctx.tracker.emit("tool:memory", "Asking the screening agent about earlier screenings...", 50)
        request_id = uuid.uuid4().hex
        ctx.running.agent_request_ids.add(request_id)
        try:
            answer = await get_container().agent.ask(ctx.agent_tenant, request_id, question[:2000])
        except AppError as e:
            answer = f"The screening agent could not answer: {e.spec.message}"
        ctx.running.agent_request_ids.discard(request_id)  # kept on cancellation (see perform_screening)
        return answer

    @tool
    async def search_documents(query: str, source: Literal["resume", "jd", "both"] = "both") -> str:
        """Search the chat's resume and/or job description for passages relevant to a question (hybrid
        semantic + keyword search). Use it for questions about the candidate's background or the JD's details.

        Args:
            query: What to look for, e.g. "experience with data pipelines" or "required certifications".
            source: resume, jd, or both.
        """
        ctx.tools_used.append(f"search_documents:{source}")
        where = "resume and JD" if source == "both" else source
        ctx.tracker.emit("tool:search", f"Searching the {where}...", 55)
        async with get_container().db.session() as session:
            chat = await ChatRepository(session, ctx.scope).get_owned(ctx.chat_id, ctx.user.user_id)
        ids = [doc_id for kind, doc_id in ((DocumentKind.RESUME, chat.resume_document_id),
                                           (DocumentKind.JD, chat.jd_document_id))
               if doc_id and source in (kind.value, "both")]
        if not ids:
            return f"No {where} document in this chat yet."
        hits = await documents.search(ctx.scope, ids, query[:500], get_container().settings.search_results)
        if not hits:
            return "Nothing relevant found in the documents."
        return "\n\n".join(f"[{h.kind} #{h.chunk_index}] {h.content}" for h in hits)

    @tool
    async def calculate(question: str) -> str:
        """Compute an exact answer with code for questions that need dates, durations, counting or arithmetic
        about the candidate: when the first (or latest) job started, total or relevant years of experience, gaps
        between jobs, time in a role, number of employers. Use it instead of working such numbers out yourself.

        Args:
            question: The calculation question in plain words, e.g. "When did the candidate start their first job?"
        """
        ctx.tools_used.append("calculate")
        ctx.tracker.emit("tool:calculate", "Calculating the exact answer with code...", 60)
        data, roles = await _calculation_data(ctx)
        if not data and not roles:
            return "No resume in this chat yet, so there is nothing to calculate from."
        c = get_container()
        return await run_calculation(c.settings, c.compute, ctx.budget, question, data, roles)

    return [screen_candidate, get_screening_details, ask_screening_agent, search_documents, calculate]


async def _calculation_data(ctx: TurnContext) -> tuple[str, list[dict]]:
    """Facts for the code author: the roles of the latest screening (structured, with dates; they become ROLES in
    the program) and, as text, education, certifications and the resume so nothing the screening missed is lost."""
    async with get_container().db.session() as session:
        chat = await ChatRepository(session, ctx.scope).get_owned(ctx.chat_id, ctx.user.user_id)
        latest = await ScreeningRepository(session, ctx.scope).latest_for_chat(ctx.chat_id)
        resume = (await DocumentRepository(session, ctx.scope).get_scoped(chat.resume_document_id)
                  if chat and chat.resume_document_id else None)
    result = latest.result if latest else {}
    parts = []
    if result.get("education"):
        parts.append("Education:\n" + "\n".join(f"- {e}" for e in result["education"]))
    if result.get("certifications"):
        parts.append("Certifications:\n" + "\n".join(f"- {e}" for e in result["certifications"]))
    if resume and resume.text:
        parts.append("Resume text:\n" + resume.text[:get_container().settings.max_calculation_resume_chars])
    return "\n\n".join(parts), list(result.get("roles") or [])
