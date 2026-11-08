"""Talent knowledge: what the orchestrator stores in, and asks of, the shared knowledge service.

* remember_profile   - resume text -> knowledge graph (on upload; background, never blocks the turn)
* remember_screening - screening outcome (score, met requirements, gaps; no JD text) -> graph, for later matching
* ask                - question -> facts from the project's graph (all candidates)
* candidate_facts    - question -> facts about ONE candidate (this chat's resume) for answer grounding

Every failure is logged and swallowed for the background steps: screening and chat keep working without the
knowledge service. `ask` returns a friendly message instead of raising.
"""
import asyncio
from typing import Optional

from agent_common.db.repositories import TenantScope
from agent_common.errors import AppError
from agent_common.knowledge.client import KnowledgeClient
from agent_common.knowledge.contracts import IngestRequest, SearchRequest, SearchResult
from agent_common.logging import get_logger, log_event

from ats_orchestrator.config import OrchestratorSettings
from ats_orchestrator.constants import GAP_GRADES, MET_GRADES
from ats_orchestrator.knowledge.guidance import load_guidance
from ats_orchestrator.knowledge.pack_guidance import GuidanceResolver

log = get_logger(__name__)
_background: set[asyncio.Task] = set()     # keeps fire-and-forget tasks referenced until they finish


def resume_source_ref(document_id: int) -> str:
    return f"document:{document_id}"


def profile_text(document_id: int, resume_text: str) -> str:
    """Resume text with a source header, so the graph records which document describes the person."""
    return f"Source: resume {resume_source_ref(document_id)}\n\n{resume_text}"


def screening_text(document_id: int, result: dict) -> str:
    """Compact, factual screening outcome for the graph. Requirement names only - never the JD text.
    The candidate is identified by the same source id as their profile, so both link to one person."""
    met = [r["name"] for r in result.get("requirements", []) if r["grade"] in MET_GRADES]
    gaps = [f"{r['name']} ({r['importance']})" for r in result.get("requirements", []) if r["grade"] in GAP_GRADES]
    return (f"Candidate: the person described in resume {resume_source_ref(document_id)}\n"
            f"Role: {result.get('roleTitle') or 'unspecified role'}\n"
            f"Score: {result['score']}/100 ({result['band']}), recommendation: {result['recommendation']}\n"
            f"Requirements met: {', '.join(met) or 'none'}\nGaps: {', '.join(gaps) or 'none'}")


class TalentKnowledge:
    def __init__(self, settings: OrchestratorSettings, client: Optional[KnowledgeClient],
                 guidance: Optional[GuidanceResolver] = None):
        self.settings, self.client = settings, client
        # The resolver yields the client's activated pack when it has a complete one, else the packaged
        # guidance. With no resolver wired (local runs, tests) the packaged guidance is used directly.
        self.resolver = guidance or GuidanceResolver(None, settings.pack_domain, settings.pack_skill_name,
                                                     load_guidance(settings.knowledge_guidance_version))
        self.guidance = self.resolver.module_guidance

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _ingest_later(self, request: IngestRequest) -> None:
        async def run() -> None:
            try:
                receipt = await self.client.ingest(request)
                log_event(log, "knowledge.ingest_sent", source_type=request.source_type,
                          episode_id=receipt.episode_id, duplicate=receipt.duplicate)
            except Exception as e:
                log_event(log, "knowledge.ingest_failed", source_type=request.source_type,
                          code=e.spec.code if isinstance(e, AppError) else None, error_type=type(e).__name__)
        task = asyncio.create_task(run())
        _background.add(task)
        task.add_done_callback(_background.discard)

    async def _ingest_resolved(self, scope: TenantScope, source_type: str, document_id: int, text: str,
                               select, *, raise_on_error: bool):
        """Resolve the client's guidance, then ingest. Returns the receipt, or None when it failed quietly.

        `select(guidance)` returns the (guidance text, ontology) this source type needs, so resume and
        screening ingests share one path while asking for different parts of the same resolved pack.

        `raise_on_error` is the whole difference between the two callers: a chat turn must not fail because
        the graph is down (the failure is logged and the turn continues), while a durable run *must* see the
        failure so Temporal can retry it.
        """
        guidance = await self.resolver.for_client(scope.client_id)
        guidance_text, ontology = select(guidance)
        request = self._request(scope, source_type, document_id, text, guidance_text, ontology)
        try:
            receipt = await self.client.ingest(request)
        except Exception as e:
            log_event(log, "knowledge.ingest_failed", source_type=source_type,
                      code=e.spec.code if isinstance(e, AppError) else None, error_type=type(e).__name__,
                      guidance_source=guidance.source)
            if raise_on_error:
                raise
            return None
        log_event(log, "knowledge.ingest_sent", source_type=source_type, episode_id=receipt.episode_id,
                  duplicate=receipt.duplicate, guidance_source=guidance.source)
        return receipt

    def _ingest_later_resolved(self, scope: TenantScope, source_type: str, document_id: int, text: str,
                               select) -> None:
        """Queue the resolve-and-ingest above in the background, so it never delays the caller's turn."""
        task = asyncio.create_task(
            self._ingest_resolved(scope, source_type, document_id, text, select, raise_on_error=False))
        _background.add(task)
        task.add_done_callback(_background.discard)

    def _request(self, scope: TenantScope, source_type: str, document_id: int, text: str, guidance: str,
                 ontology: Optional[dict] = None) -> IngestRequest:
        """Ingest request about the candidate of one resume (the resume document is source and subject)."""
        ref = resume_source_ref(document_id)
        return IngestRequest(client_id=scope.client_id, project_id=scope.project_id,
                             namespace=self.settings.knowledge_namespace, source_type=source_type, source_ref=ref,
                             subject_ref=ref, text=text[: self.settings.knowledge_max_text_chars],
                             guidance=guidance, ontology=ontology)

    def remember_profile(self, scope: TenantScope, document_id: int, resume_text: str) -> None:
        """Queue the resume for the project's knowledge graph (idempotent: re-uploads are de-duplicated).

        The client's pack is resolved inside the background task, so looking it up never delays the turn that
        uploaded the resume.
        """
        if self.enabled and resume_text.strip():
            text = profile_text(document_id, resume_text)
            self._ingest_later_resolved(
                scope, "resume", document_id, text,
                lambda guidance: (guidance.profile_extraction, guidance.ontology))

    async def remember_profile_now(self, scope: TenantScope, document_id: int, resume_text: str) -> dict:
        """The awaited form of `remember_profile`, for the durable onboarding journey.

        A workflow activity owns its own retry, so this must not swallow a failure the way the background
        path does: it raises, Temporal retries with backoff, and the graph de-duplicates on the content hash
        so the retry costs nothing extra. Returns `{"episodeId", "duplicate"}`, or a `skipped` reason when
        there is no graph configured or nothing to ingest.
        """
        if not self.enabled:
            return {"episodeId": "", "duplicate": False, "skipped": "no knowledge graph configured"}
        if not resume_text.strip():
            return {"episodeId": "", "duplicate": False, "skipped": "the document has no text"}
        receipt = await self._ingest_resolved(
            scope, "resume", document_id, profile_text(document_id, resume_text),
            lambda guidance: (guidance.profile_extraction, guidance.ontology), raise_on_error=True)
        return {"episodeId": receipt.episode_id, "duplicate": bool(receipt.duplicate)}

    def remember_screening(self, scope: TenantScope, document_id: int, result: dict) -> None:
        """Queue the screening outcome, linked to the same candidate (source = the resume document)."""
        if self.enabled and self.settings.knowledge_ingest_screenings:
            text = screening_text(document_id, result)
            self._ingest_later_resolved(
                scope, "screening", document_id, text,
                lambda guidance: (guidance.screening_extraction, None))

    async def ask(self, scope: TenantScope, question: str, request_id: str) -> str:
        """Graph facts for the assistant to answer from (facts only: no extra LLM call in the knowledge service,
        the assistant already writes the reply). A clear message when the graph has nothing yet or fails."""
        if not self.enabled:
            return "The candidate knowledge graph is not enabled."
        try:
            result = await self.client.search(SearchRequest(
                client_id=scope.client_id, project_id=scope.project_id, namespace=self.settings.knowledge_namespace,
                question=question[:2000], guidance=(await self.resolver.for_client(scope.client_id)).answering,
                request_id=request_id,
                limit=self.settings.knowledge_search_facts, answer=False))
        except AppError as e:
            return f"The knowledge graph could not answer: {e.spec.message} {e.spec.hint}"
        return format_facts(result)

    async def candidate_facts(self, scope: TenantScope, document_id: int, question: str) -> str:
        """Graph facts about the candidate of one resume, for grounding a chat answer. Returns "" when the graph
        has nothing for them yet, is disabled, slow or failing: grounding must never block or break a turn."""
        if not self.enabled:
            return ""
        request = SearchRequest(client_id=scope.client_id, project_id=scope.project_id,
                                namespace=self.settings.knowledge_namespace, question=question[:2000],
                                subject_refs=[resume_source_ref(document_id)], answer=False,
                                limit=self.settings.knowledge_grounding_facts)
        try:
            result = await asyncio.wait_for(self.client.search(request), self.settings.knowledge_grounding_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # timeout, unavailable, invalid: the turn goes on with document excerpts only
            log_event(log, "knowledge.grounding_skipped", error_type=type(e).__name__,
                      code=e.spec.code if isinstance(e, AppError) else None)
            return ""
        return format_candidate(result)

    async def cancel(self, request_id: str) -> None:
        if self.enabled:
            await self.client.cancel(request_id)


GRAPH_EMPTY = ("The candidate knowledge graph has no facts for this yet. New profiles take several minutes to be "
               "added in the background. For the resume in this chat, use search_documents instead.")


def format_facts(result: SearchResult) -> str:
    """Facts (current first, superseded marked) and entity summaries as a compact tool result."""
    if not result.facts and not result.entities:
        return GRAPH_EMPTY
    lines = [f"- {f.fact}" + (" (no longer true)" if f.invalid_at else "")
             for f in sorted(result.facts, key=lambda f: f.invalid_at is not None)]
    lines += [f"- {e.name}: {e.summary}" for e in result.entities if e.summary][:5]
    return "Facts from the candidate knowledge graph (quote them; do not add anything else):\n" + "\n".join(lines)


def format_candidate(result: SearchResult) -> str:
    """One candidate's graph knowledge as prompt lines: entity attributes (exact values such as location or email),
    then current facts. "" when there is nothing."""
    lines = []
    for entity in result.entities:
        values = "; ".join(f"{key}: {value}" for key, value in entity.attributes.items() if value not in (None, "", []))
        kind = "/".join(entity.labels) or "entity"
        if values:
            lines.append(f"- {entity.name} ({kind}): {values}")
    lines += [f"- {fact.fact}" for fact in result.facts if not fact.invalid_at]
    return "\n".join(lines)
