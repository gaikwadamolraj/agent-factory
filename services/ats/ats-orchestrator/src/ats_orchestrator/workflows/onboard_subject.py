"""J1 - onboard a subject: source document -> parsed -> scanned -> subject record -> indexed -> graph.

**This module defines workflows, so it must stay importable and deterministic.** The Temporal sandbox
re-imports it on every worker start; anything non-deterministic at import time (clients, settings, network,
random, clocks) breaks workflow validation. Activities live in `activities.py` and do all the real work, which
is also what keeps the business logic framework-free and testable without Temporal (CLAUDE.md section 2).

The stages mirror docs/impl_multi_domain.md section 5 - one pipeline shape, pack-supplied profiles - so a
resume, a policy PDF, a bank statement and a media brief run this same code path. Only the pack differs.

Payloads are references throughout: a document id, a subject ref, a hash. Content lives in the blob store, the
database and the graph, never in workflow history (`agent_common.workflow.check_payload` enforces the limit).
"""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# Only literals come in here: `agent_common.constants` imports nothing but `enum`, so the sandbox re-imports
# it happily, and the workflow and the projection writer cannot disagree on how a status is spelled.
from agent_common.constants import RunStatus

# Stage names, in order. They are the vocabulary of progress: the query below reports one of these, the
# workflow_runs projection stores it, and the UI shows it per document.
STAGES = ("queued", "parsing", "scanning", "extracting", "indexing", "enriching", "published")

# Activities that only read or re-derive are safe to retry; the ones that write are made idempotent by their
# own natural keys (subject external_ref, episode content hash), so retrying them is also safe.
DEFAULT_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                            maximum_interval=timedelta(seconds=60), maximum_attempts=4)

# The projection is a dashboard row, not the work. It retries briefly and then gives up, because failing a
# finished onboarding over its status row would destroy real work to protect a display (see
# agent_common.workflow.projection). Registered by the worker from `agent_common.workflow.projection`.
RECORD_RUN_STATUS = "record_run_status"
PROJECTION_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                               maximum_interval=timedelta(seconds=15), maximum_attempts=4)

GENERIC_FAILURE = "WFL-INT-0001"      # when an activity reported no platform error code of its own


def _error_code(error: BaseException) -> str:
    """The platform error code an activity reported, or the generic one.

    An activity raising `ApplicationError(type="FILE-NFD-0001", ...)` reaches the workflow wrapped in an
    `ActivityError`, with the original as its cause. Only a well-formed `{DOMAIN}-{CATEGORY}-{NNNN}` is
    accepted, so an SDK-supplied type can never end up in the column as if it were ours.
    """
    reported = str(getattr(getattr(error, "cause", None), "type", "") or "")
    parts = reported.split("-")
    if len(parts) == 3 and parts[0].isalpha() and parts[1].isalpha() and parts[2].isdigit():
        return reported[:32]
    return GENERIC_FAILURE


@dataclass
class OnboardInput:
    """What the workflow needs: identifiers and the pack to run under - never document content."""

    client_id: int
    project_id: int
    domain: str                 # pack id, e.g. "ats"
    subject_type: str           # pack-defined, e.g. "candidate"
    document_id: int
    external_ref: str           # the caller's id for the subject; with subject_type it is the graph subject_ref
    display_name: str = ""
    guidance_version: str = ""  # pins the pack version for reproducibility; empty = the client's active pack


@dataclass
class OnboardResult:
    """A small summary. Anything large becomes an artifact and is referenced, not returned."""

    subject_ref: str
    stage: str
    chunks: int = 0
    episode_id: str = ""
    flagged: bool = False
    notes: list[str] = field(default_factory=list)


@workflow.defn(name="onboard_subject")
class OnboardSubject:
    """One subject onboarded, durably and idempotently.

    Idempotent by construction: the workflow id is derived from the document, so re-submitting reattaches;
    and each writing activity keys on a natural id, so a replay after a worker crash does not duplicate the
    subject, the chunks or the graph episode.
    """

    def __init__(self) -> None:
        self._stage = "queued"
        self._notes: list[str] = []

    @workflow.run
    async def run(self, request: OnboardInput) -> OnboardResult:
        subject_ref = f"{request.subject_type}:{request.external_ref}"
        await self._project(request, RunStatus.RUNNING.value)
        try:
            return await self._onboard(request, subject_ref)
        except Exception as error:
            # A failure Temporal will not retry any further becomes the run's recorded outcome. Only the
            # error code and the stage are recorded: never the exception text, which can quote document
            # content or internals (CLAUDE.md section 5).
            await self._project(request, RunStatus.FAILED.value, error_code=_error_code(error),
                                error_summary=f"onboarding failed during stage '{self._stage}'")
            raise
        # asyncio.CancelledError is deliberately not caught: a cancelled workflow may not start further
        # activities, so `JourneyLauncher.cancel` writes that transition on the canceller's side instead.

    async def _onboard(self, request: OnboardInput, subject_ref: str) -> OnboardResult:
        """The pipeline itself, so the lifecycle reporting above stays readable."""
        self._stage = "parsing"
        parsed = await workflow.execute_activity(
            "parse_document", request, start_to_close_timeout=timedelta(minutes=10), retry_policy=DEFAULT_RETRY)

        self._stage = "scanning"
        scan = await workflow.execute_activity(
            "scan_document", {"clientId": request.client_id, "projectId": request.project_id,
                              "documentId": request.document_id, "textHash": parsed.get("textHash", "")},
            start_to_close_timeout=timedelta(minutes=5), retry_policy=DEFAULT_RETRY)
        flagged = bool(scan.get("flagged"))
        if flagged:
            # A source that tries to instruct the AI is recorded and carried forward, never silently dropped:
            # downstream grading must be able to see it (agent_skill screening does the same today).
            self._notes.append("source flagged by the injection scan")

        self._stage = "extracting"
        subject = await workflow.execute_activity(
            "upsert_subject", {"request": request, "subjectRef": subject_ref, "flagged": flagged},
            start_to_close_timeout=timedelta(minutes=10), retry_policy=DEFAULT_RETRY)

        self._stage = "indexing"
        indexed = await workflow.execute_activity(
            "index_document", {"clientId": request.client_id, "projectId": request.project_id,
                               "documentId": request.document_id},
            start_to_close_timeout=timedelta(minutes=20), retry_policy=DEFAULT_RETRY)

        self._stage = "enriching"
        episode = await workflow.execute_activity(
            "enrich_graph", {"request": request, "subjectRef": subject_ref},
            start_to_close_timeout=timedelta(minutes=30), retry_policy=DEFAULT_RETRY)

        self._stage = "published"
        result = OnboardResult(subject_ref=subject.get("subjectRef", subject_ref), stage=self._stage,
                               chunks=int(indexed.get("chunks", 0)),
                               episode_id=str(episode.get("episodeId", "")),
                               flagged=flagged, notes=list(self._notes))
        await self._project(request, RunStatus.COMPLETED.value, subject_id=subject.get("subjectId"),
                            result_summary={"subjectRef": result.subject_ref, "stage": result.stage,
                                            "chunks": result.chunks, "episodeId": result.episode_id,
                                            "flagged": result.flagged, "notes": result.notes})
        return result

    async def _project(self, request: OnboardInput, status: str, *, subject_id: Optional[int] = None,
                       result_summary: Optional[dict] = None, error_code: str = "",
                       error_summary: str = "") -> None:
        """Report this run's lifecycle to `workflow_runs`, without ever failing the run over it.

        `workflow.info().workflow_id` is the key the launcher wrote the row under, and it is deterministic,
        so this is replay-safe. The payload shape is the one documented on
        `agent_common.workflow.projection.RunProjection.record_run_status`.
        """
        payload: dict[str, Any] = {"clientId": request.client_id, "status": status,
                                   "workflowId": workflow.info().workflow_id}
        if subject_id:
            payload["subjectId"] = int(subject_id)
        if result_summary:
            payload["resultSummary"] = result_summary
        if error_code:
            payload["errorCode"] = error_code
        if error_summary:
            payload["errorSummary"] = error_summary
        try:
            await workflow.execute_activity(RECORD_RUN_STATUS, payload,
                                            start_to_close_timeout=timedelta(seconds=30),
                                            retry_policy=PROJECTION_RETRY)
        except Exception:
            # Deliberate: the dashboard row falls behind, the run and its real output do not. The engine
            # still has the truth, and `WorkflowClient.describe` reads it.
            self._notes.append("run status could not be recorded")

    @workflow.query(name="stage")
    def stage(self) -> str:
        """Progress without touching the database - what a polling UI reads."""
        return self._stage

    @workflow.query(name="notes")
    def notes(self) -> list[str]:
        return list(self._notes)


def workflow_classes() -> list[type]:
    """Everything a worker on the onboarding queue must register."""
    return [OnboardSubject]
