"""The activities of the onboarding workflow: all the work, none of the orchestration.

Activities are plain async methods on a class holding its dependencies, so every one of them can be called
and asserted on without Temporal running (CLAUDE.md section 2: business logic stays framework-free). The
`@activity.defn` decorators only name them for the worker.

Two properties the workflow relies on:

* **Idempotent writes.** Each writing activity keys on a natural id - the subject's `(domain, subject_type,
  external_ref)`, the graph episode's content hash - so a replay after a worker crash updates rather than
  duplicates. This is what makes a durable retry safe.
* **Cancellation propagates.** Nothing here swallows `asyncio.CancelledError`: a cancelled run must stop the
  MCP call and the LLM stream underneath it, so an abort stops costing money at once (CLAUDE.md section 6).
"""
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agent_common.db import Subject, SubjectRepository, SubjectVersion, SubjectVersionRepository
from agent_common.logging import get_logger, log_event

log = get_logger(__name__)

# `ApplicationError.type` carries the platform error code out of the activity, so the workflow can record the
# real reason on `workflow_runs` instead of a generic internal failure (CLAUDE.md section 5).
DOCUMENT_UNAVAILABLE = "FILE-NFD-0001"


def _as_dict(value: Any) -> dict:
    """Activity arguments arrive as dataclasses or plain dicts depending on the caller; normalise once."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return dict(value or {})


class OnboardActivities:
    """Dependencies in, activities out.

    Every dependency is optional: a deployment without the document service, the guardrail or the knowledge
    graph still onboards a subject, it just records less. That keeps the pipeline usable while the platform is
    assembled, and it is the same degradation rule the rest of the orchestrator follows.
    """

    def __init__(self, database, documents=None, guardrail=None, knowledge=None,
                 max_text_chars: int = 100_000) -> None:
        self._database = database
        self._documents, self._guardrail, self._knowledge = documents, guardrail, knowledge
        self._max_text_chars = max_text_chars

    # ----------------------------------------------------------------- parse
    @activity.defn(name="parse_document")
    async def parse_document(self, request: Any) -> dict:
        """Extract text from the source document. Returns a hash and a size, never the text itself.

        Fails the run, without retrying, when the document is not readable by this client and project. The
        read is tenant-scoped, so "not found" also covers "belongs to someone else" - and publishing an empty
        subject in that case would be worse than failing: it would look like a successful onboarding.
        """
        data = _as_dict(request)
        if self._documents is None:
            return {"textHash": "", "chars": 0, "skipped": "no document service configured"}
        parsed = await self._documents.read(client_id=data["client_id"], project_id=data["project_id"],
                                            document_id=data["document_id"])
        if not parsed:
            raise ApplicationError(f"document {data['document_id']} is not available in this project",
                                   type=DOCUMENT_UNAVAILABLE, non_retryable=True)
        text = parsed.get("text", "")[: self._max_text_chars]
        log_event(log, "onboard.parsed", document_id=data["document_id"], chars=len(text))
        return {"textHash": parsed.get("textHash", ""), "chars": len(text)}

    # ------------------------------------------------------------------ scan
    @activity.defn(name="scan_document")
    async def scan_document(self, payload: Any) -> dict:
        """Injection-scan the extracted text. A flagged source is recorded, never silently dropped."""
        data = _as_dict(payload)
        if self._guardrail is None:
            return {"flagged": False, "skipped": "no guardrail configured"}
        verdict = await self._guardrail.check_document(client_id=data["clientId"],
                                                       project_id=data["projectId"],
                                                       document_id=data["documentId"])
        flagged = bool((verdict or {}).get("flagged"))
        if flagged:
            log_event(log, "onboard.flagged", document_id=data["documentId"])
        return {"flagged": flagged}

    # --------------------------------------------------------------- subject
    @activity.defn(name="upsert_subject")
    async def upsert_subject(self, payload: Any) -> dict:
        """Create or update the subject row, and snapshot its payload.

        Idempotent on `(client, project, domain, subject_type, external_ref)`: a replay updates the same row
        and adds one more immutable snapshot, so history stays complete without duplicating the record.
        """
        data = _as_dict(payload)
        request = _as_dict(data.get("request"))
        client_id, project_id = int(request["client_id"]), int(request["project_id"])
        async with self._database.tenant_session(client_id) as session:
            subjects = SubjectRepository(session)
            existing = await subjects.get_by_ref(client_id, project_id, request["domain"],
                                                 request["subject_type"], request["external_ref"])
            payload_body = {"documentId": request["document_id"], "flagged": bool(data.get("flagged")),
                            "onboardedAt": datetime.now(timezone.utc).isoformat()}
            if existing is None:
                subject = Subject(client_id=client_id, project_id=project_id, domain=request["domain"],
                                  subject_type=request["subject_type"], external_ref=request["external_ref"],
                                  display_name=request.get("display_name") or "", payload=payload_body)
                await subjects.add(subject)
                created = True
            else:
                subject = existing
                subject.payload = {**(subject.payload or {}), **payload_body}
                subject.updated_at = datetime.now(timezone.utc)
                created = False
            await session.flush()

            versions = SubjectVersionRepository(session)
            number = await versions.next_version_number(client_id, subject.id)
            await versions.add(SubjectVersion(client_id=client_id, subject_id=subject.id, version_number=number,
                                              payload_snapshot=dict(subject.payload or {}),
                                              change_reason="onboard_subject"))
        log_event(log, "onboard.subject_upserted", subject_ref=data.get("subjectRef"), created=created,
                  version=number)
        return {"subjectRef": data.get("subjectRef", ""), "subjectId": subject.id, "created": created,
                "version": number}

    # ----------------------------------------------------------------- index
    @activity.defn(name="index_document")
    async def index_document(self, payload: Any) -> dict:
        """Chunk and embed the document for retrieval. Returns a count, never the chunks."""
        data = _as_dict(payload)
        if self._documents is None:
            return {"chunks": 0, "skipped": "no document service configured"}
        result = await self._documents.index(client_id=data["clientId"], project_id=data["projectId"],
                                             document_id=data["documentId"])
        chunks = int((result or {}).get("chunks", 0))
        log_event(log, "onboard.indexed", document_id=data["documentId"], chunks=chunks)
        return {"chunks": chunks}

    # ---------------------------------------------------------------- enrich
    @activity.defn(name="enrich_graph")
    async def enrich_graph(self, payload: Any) -> dict:
        """Ingest the source into the knowledge graph under the pack's ontology and guidance.

        The graph de-duplicates on a content hash of (text, guidance, ontology), so a replay is a no-op rather
        than a second extraction - which is the expensive part.
        """
        data = _as_dict(payload)
        request = _as_dict(data.get("request"))
        if self._knowledge is None:
            return {"episodeId": "", "skipped": "no knowledge graph configured"}
        receipt = await self._knowledge.ingest_subject(client_id=int(request["client_id"]),
                                                       project_id=int(request["project_id"]),
                                                       document_id=int(request["document_id"]),
                                                       subject_ref=data.get("subjectRef", ""))
        episode_id = str((receipt or {}).get("episodeId", ""))
        log_event(log, "onboard.enriched", subject_ref=data.get("subjectRef"), episode_id=episode_id,
                  duplicate=bool((receipt or {}).get("duplicate")))
        return {"episodeId": episode_id, "duplicate": bool((receipt or {}).get("duplicate"))}

    def all(self) -> list:
        """Everything a worker on the onboarding queue must register."""
        return [self.parse_document, self.scan_document, self.upsert_subject, self.index_document,
                self.enrich_graph]
