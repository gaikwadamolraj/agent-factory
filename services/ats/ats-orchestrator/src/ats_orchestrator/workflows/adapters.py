"""Adapters from the onboarding activities' narrow interfaces onto this service's existing stores.

The activities in `activities.py` are domain-neutral on purpose: they ask for a document to be read, scanned,
indexed and ingested, and know nothing about resumes, chunk tables or the guardrail's API. These adapters are
where that abstraction meets the ATS implementation, which is what lets the same workflow run an insurance
policy or a bank statement later by swapping this file's equivalents - not by editing the workflow.

Everything here is tenant-scoped through `TenantScope`, so a worker processing one client's queue entry can
never touch another's rows (CLAUDE.md section 7).
"""
import hashlib
from typing import Optional

from agent_common.db import Database
from agent_common.db.repositories import TenantScope
from agent_common.guardrails.client import TextSource
from agent_common.llm import embedding_id
from agent_common.logging import get_logger, log_event

from ats_orchestrator.config import OrchestratorSettings
from ats_orchestrator.knowledge import TalentKnowledge
from ats_orchestrator.repositories import ChunkRepository, DocumentRepository
from ats_orchestrator.services.documents import embed_chunks
from ats_orchestrator.services.documents.chunking import chunk_text

log = get_logger(__name__)


def _current_embedding_id() -> Optional[str]:
    """The embedding identity in use, or None when no embedding model is configured at all.

    None means "do not skip anything": the indexing path below degrades on its own (chunks without vectors,
    keyword search), and a missing identity must never be mistaken for "already indexed".
    """
    try:
        return embedding_id()
    except Exception:
        return None


class DocumentAccess:
    """Reads and re-indexes a stored document, for the `parse_document` and `index_document` activities.

    Onboarding runs *after* upload, so the text is already extracted and stored; this reads it back rather
    than re-parsing the original file, which keeps the workflow cheap to retry. Indexing is idempotent:
    `replace_chunks` deletes the document's chunks before writing the new ones, so a replay leaves exactly
    one set.
    """

    def __init__(self, database: Database, settings: OrchestratorSettings) -> None:
        self._database, self._settings = database, settings

    async def read(self, *, client_id: int, project_id: int, document_id: int) -> dict:
        """The document's text, capped, plus its stored hash. Returns `{}` when the document is gone."""
        async with self._database.session() as session:
            document = await DocumentRepository(session, TenantScope(client_id, project_id)) \
                .get_scoped(document_id)
            if document is None:
                return {}
            text = (document.text or "")[: self._settings.knowledge_max_text_chars]
            text_hash = document.text_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {"text": text, "textHash": text_hash}

    async def index(self, *, client_id: int, project_id: int, document_id: int) -> dict:
        """Chunk, embed and store the document's chunks. Returns the chunk count, never the chunks.

        Skips the work when the document is already searchable at the embedding identity in use today. The
        upload path indexes synchronously (a chat turn must be able to search the resume it just received),
        so onboarding would otherwise pay for the same embeddings a second time. A replay after a model
        change, or after an upload whose embeddings failed, still re-indexes.
        """
        scope = TenantScope(client_id, project_id)
        async with self._database.session() as session:
            document = await DocumentRepository(session, scope).get_scoped(document_id)
            text = (document.text or "") if document is not None else ""
            model = _current_embedding_id()
            if document is not None and text.strip() and model:
                existing = await ChunkRepository(session, scope).indexed_count(document_id, model)
                if existing:
                    return {"chunks": existing, "skipped": "already indexed at the current embedding model"}
        if document is None or not text.strip():
            return {"chunks": 0, "skipped": "the document has no text"}
        chunks = chunk_text(text)
        # Embeddings are best-effort by design: without them hybrid search falls back to keywords, which is
        # far better than refusing to onboard the subject at all (services/documents/__init__.py does the same).
        vectors, model = await embed_chunks(chunks)
        async with self._database.session() as session:
            await ChunkRepository(session, scope).replace_chunks(document, chunks, vectors, model)
        log_event(log, "onboard.chunks_written", document_id=document_id, chunks=len(chunks),
                  embedded=bool(vectors))
        return {"chunks": len(chunks), "embedded": bool(vectors)}


class GuardrailScan:
    """Injection-scans a stored document for the `scan_document` activity.

    The upload path already refuses a document whose text tries to instruct the AI; this is the second check,
    for sources that arrive another way (a connector, a bulk import) and for the record. It reports rather
    than refuses, because by this point the subject is being onboarded and the honest outcome is a flagged
    record, not a silent drop (docs/security_llm.md).
    """

    def __init__(self, database: Database, guardrails) -> None:
        self._database, self._guardrails = database, guardrails

    async def check_document(self, *, client_id: int, project_id: int, document_id: int) -> dict:
        async with self._database.session() as session:
            document = await DocumentRepository(session, TenantScope(client_id, project_id)) \
                .get_scoped(document_id)
            text = (document.text or "") if document is not None else ""
        if not text.strip():
            return {"flagged": False}
        result = await self._guardrails.check(text, TextSource.DOCUMENT)
        if result.blocked:
            log_event(log, "security.document_flagged", document_id=document_id, reasons=result.reasons,
                      detector=result.detector)
        return {"flagged": bool(result.blocked), "detector": result.detector or ""}


class KnowledgeIngest:
    """Ingests the document into the knowledge graph for the `enrich_graph` activity.

    It reads the text itself rather than receiving it, because workflow payloads carry references only
    (`agent_common.workflow.MAX_PAYLOAD_BYTES`): a resume would not fit in workflow history and does not
    belong there.
    """

    def __init__(self, database: Database, knowledge: TalentKnowledge, settings: OrchestratorSettings) -> None:
        self._database, self._knowledge, self._settings = database, knowledge, settings

    async def ingest_subject(self, *, client_id: int, project_id: int, document_id: int,
                             subject_ref: str = "") -> dict:
        scope = TenantScope(client_id, project_id)
        async with self._database.session() as session:
            document = await DocumentRepository(session, scope).get_scoped(document_id)
            text = (document.text or "") if document is not None else ""
        receipt = await self._knowledge.remember_profile_now(scope, document_id, text)
        log_event(log, "onboard.graph_ingested", document_id=document_id, subject_ref=subject_ref,
                  episode_id=receipt.get("episodeId", ""), duplicate=receipt.get("duplicate", False))
        return receipt


def build_onboard_dependencies(database: Database, settings: OrchestratorSettings, guardrails,
                               knowledge: TalentKnowledge) -> dict:
    """The keyword arguments `OnboardActivities` takes, built from the container's objects.

    Kept in one function so the worker entrypoint stays a wiring file and the mapping between the generic
    activity interfaces and this service's implementations is visible in one place.
    """
    return {"documents": DocumentAccess(database, settings),
            "guardrail": GuardrailScan(database, guardrails),
            "knowledge": KnowledgeIngest(database, knowledge, settings),
            "max_text_chars": settings.knowledge_max_text_chars}
