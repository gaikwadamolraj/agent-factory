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

from ats_common.db.repositories import TenantScope
from ats_common.errors import AppError
from ats_common.knowledge.client import KnowledgeClient
from ats_common.knowledge.contracts import IngestRequest, SearchRequest, SearchResult
from ats_common.logging import get_logger, log_event

from orchestrator.config import OrchestratorSettings
from orchestrator.constants import GAP_GRADES, MET_GRADES
from orchestrator.knowledge.guidance import load_guidance, load_ontology

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
    def __init__(self, settings: OrchestratorSettings, client: Optional[KnowledgeClient]):
        self.settings, self.client = settings, client
        self.guidance = load_guidance(settings.knowledge_guidance_version)

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

    def _request(self, scope: TenantScope, source_type: str, document_id: int, text: str, guidance: str,
                 ontology: Optional[dict] = None) -> IngestRequest:
        """Ingest request about the candidate of one resume (the resume document is source and subject)."""
        ref = resume_source_ref(document_id)
        return IngestRequest(client_id=scope.client_id, project_id=scope.project_id,
                             namespace=self.settings.knowledge_namespace, source_type=source_type, source_ref=ref,
                             subject_ref=ref, text=text[: self.settings.knowledge_max_text_chars],
                             guidance=guidance, ontology=ontology)

    def remember_profile(self, scope: TenantScope, document_id: int, resume_text: str) -> None:
        """Queue the resume for the project's knowledge graph (idempotent: re-uploads are de-duplicated)."""
        if self.enabled and resume_text.strip():
            self._ingest_later(self._request(scope, "resume", document_id, profile_text(document_id, resume_text),
                                             self.guidance.profile_extraction,
                                             load_ontology(self.guidance.profile_ontology)))

    def remember_screening(self, scope: TenantScope, document_id: int, result: dict) -> None:
        """Queue the screening outcome, linked to the same candidate (source = the resume document)."""
        if self.enabled and self.settings.knowledge_ingest_screenings:
            self._ingest_later(self._request(scope, "screening", document_id, screening_text(document_id, result),
                                             self.guidance.screening_extraction))

    async def ask(self, scope: TenantScope, question: str, request_id: str) -> str:
        """Graph facts for the assistant to answer from (facts only: no extra LLM call in the knowledge service,
        the assistant already writes the reply). A clear message when the graph has nothing yet or fails."""
        if not self.enabled:
            return "The candidate knowledge graph is not enabled."
        try:
            result = await self.client.search(SearchRequest(
                client_id=scope.client_id, project_id=scope.project_id, namespace=self.settings.knowledge_namespace,
                question=question[:2000], guidance=self.guidance.answering, request_id=request_id,
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
