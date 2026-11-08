"""J3 - bulk match: one criteria set against many subjects, graded, ranked, shortlisted.

**This module defines a workflow, so it must stay importable and deterministic.** The Temporal sandbox
re-imports it on every worker start; anything non-deterministic at import time (clients, settings, network,
random, clocks) breaks workflow validation. All the work is in `activities.py`.

The shape is domain-neutral (docs/impl_multi_domain.md section 4): one job description against a thousand
candidates, one exclusion clause against open claims, one sanctions typology against a customer base. Only
the pack differs.

Three properties this workflow exists to provide, which a loop in a request handler cannot:

* **It is bounded.** Subjects are selected in pages and graded `concurrency` at a time, with a hard
  `max_subjects` ceiling. A bulk run must never be able to open ten thousand concurrent LLM calls.
* **It is resumable.** Each subject's grade is written under `(run, subject)`, so a worker that dies
  half-way through re-grades only what it had not finished - the expensive part is not repeated.
* **One subject's failure is not the run's failure.** A candidate whose resume will not parse is recorded
  with its error code and the run carries on. A bulk run that silently skipped two hundred subjects would
  look exactly like one that found them unsuitable, which is the worst outcome available here.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# Only literals: `agent_common.constants` imports nothing but `enum`, so the sandbox re-imports it happily.
from agent_common.constants import RunStatus

STAGES = ("queued", "selecting", "grading", "ranking", "published")

DEFAULT_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                            maximum_interval=timedelta(seconds=60), maximum_attempts=4)
# Grading one subject costs an LLM call, so it retries less eagerly than a read: a permanent failure should
# be recorded and the run should move on rather than paying three more times to learn the same thing.
GRADE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                          maximum_interval=timedelta(seconds=120), maximum_attempts=3)

RECORD_RUN_STATUS = "record_run_status"
PROJECTION_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                               maximum_interval=timedelta(seconds=15), maximum_attempts=4)

GENERIC_FAILURE = "WFL-INT-0001"


def _error_code(error: BaseException) -> str:
    """The platform error code an activity reported, or the generic one.

    Only a well-formed `{DOMAIN}-{CATEGORY}-{NNNN}` is accepted, so an SDK-supplied type can never end up in
    the column as if it were ours.
    """
    reported = str(getattr(getattr(error, "cause", None), "type", "") or "")
    parts = reported.split("-")
    if len(parts) == 3 and parts[0].isalpha() and parts[1].isalpha() and parts[2].isdigit():
        return reported[:32]
    return GENERIC_FAILURE


@dataclass
class BulkMatchInput:
    """What the run needs: identifiers, the pack, and the bounds. Never any document content."""

    client_id: int
    project_id: int
    domain: str                      # pack id, e.g. "ats"
    subject_type: str                # pack-defined, e.g. "candidate"
    criteria_ref: str                # what the subjects are graded against, by reference (e.g. "document:7")
    criteria_key: str = ""           # the pack's criteria set key
    rubric_key: str = ""             # the pack's rubric key; empty = the pack's default
    guidance_version: str = ""       # pins the pack version so a re-run is comparable
    top_n: int = 50                  # how many results the summary carries; the rest stay in match_results
    max_subjects: int = 1000         # hard ceiling on one run's work
    page_size: int = 50              # subjects fetched per selection activity
    concurrency: int = 8             # subjects graded at once; the real throttle on LLM load


@dataclass
class BulkMatchResult:
    """A small summary. The per-subject grades live in `match_results`, referenced not returned."""

    stage: str
    selected: int = 0
    graded: int = 0
    failed: int = 0
    shortlist: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@workflow.defn(name="bulk_match")
class BulkMatch:
    """One criteria set against many subjects, bounded and resumable."""

    def __init__(self) -> None:
        self._stage = "queued"
        self._selected = 0
        self._graded = 0
        self._failed = 0
        self._notes: list[str] = []

    @workflow.run
    async def run(self, request: BulkMatchInput) -> BulkMatchResult:
        await self._project(request, RunStatus.RUNNING.value)
        try:
            return await self._match(request)
        except Exception as error:
            await self._project(request, RunStatus.FAILED.value, error_code=_error_code(error),
                                error_summary=f"bulk match failed during stage '{self._stage}' after "
                                              f"{self._graded} graded and {self._failed} failed")
            raise
        # asyncio.CancelledError is deliberately not caught: a cancelled workflow may not start further
        # activities, so `JourneyLauncher.cancel` writes that transition on the canceller's side.

    async def _match(self, request: BulkMatchInput) -> BulkMatchResult:
        """Select, grade, rank. Kept apart from `run` so the lifecycle reporting above stays readable."""
        self._stage = "selecting"
        cursor: Optional[int] = None
        remaining = max(0, int(request.max_subjects))

        self._stage = "grading"
        while remaining > 0:
            page = await workflow.execute_activity(
                "select_subjects",
                {"clientId": request.client_id, "projectId": request.project_id, "domain": request.domain,
                 "subjectType": request.subject_type, "cursor": cursor,
                 "limit": min(request.page_size, remaining)},
                start_to_close_timeout=timedelta(minutes=2), retry_policy=DEFAULT_RETRY)
            subject_ids = [int(value) for value in page.get("subjectIds", [])]
            if not subject_ids:
                break
            self._selected += len(subject_ids)
            remaining -= len(subject_ids)
            await self._grade_all(request, subject_ids)
            cursor = page.get("nextCursor")
            if cursor is None:
                break

        if self._selected == 0:
            # Nothing matched the selector. Reported, not treated as success with an empty shortlist: the
            # difference between "no candidates" and "no candidates *suitable*" is the whole answer.
            self._notes.append("no subjects matched the selection")

        self._stage = "ranking"
        ranked = await workflow.execute_activity(
            "rank_results",
            {"clientId": request.client_id, "projectId": request.project_id, "topN": request.top_n},
            start_to_close_timeout=timedelta(minutes=5), retry_policy=DEFAULT_RETRY)

        self._stage = "published"
        result = BulkMatchResult(stage=self._stage, selected=self._selected, graded=self._graded,
                                 failed=self._failed, shortlist=list(ranked.get("shortlist", [])),
                                 notes=list(self._notes))
        await self._project(request, RunStatus.COMPLETED.value,
                            result_summary={"stage": result.stage, "selected": result.selected,
                                            "graded": result.graded, "failed": result.failed,
                                            "shortlist": result.shortlist, "notes": result.notes})
        return result

    async def _grade_all(self, request: BulkMatchInput, subject_ids: list[int]) -> None:
        """Grade one page of subjects, `concurrency` at a time.

        The window is what stops a thousand-candidate run becoming a thousand concurrent LLM requests. It is
        a workflow-level bound on top of the worker's own `max_concurrent_activities`, because the two
        answer different questions: how much this *run* may use, and how much this *replica* may.
        """
        window = max(1, int(request.concurrency))
        for start in range(0, len(subject_ids), window):
            batch = subject_ids[start:start + window]
            # `asyncio.gather` under Temporal's deterministic event loop: the ordering is replayable, so this
            # is the sanctioned way to fan out inside a workflow. `_grade_one` never raises, so one subject
            # cannot cancel the rest of its batch.
            await asyncio.gather(*(self._grade_one(request, subject_id) for subject_id in batch))

    async def _grade_one(self, request: BulkMatchInput, subject_id: int) -> None:
        """Grade one subject, recording a failure rather than letting it end the run.

        Temporal has already retried transient failures by the time this raises, so reaching the except
        branch means the subject genuinely cannot be graded now. That is written to its row with the error
        code, and the run continues - which is the behaviour a shortlist of 998 out of 1000 needs.
        """
        payload = {"clientId": request.client_id, "projectId": request.project_id,
                   "subjectId": subject_id, "domain": request.domain,
                   "criteriaRef": request.criteria_ref, "criteriaKey": request.criteria_key,
                   "rubricKey": request.rubric_key, "guidanceVersion": request.guidance_version}
        try:
            await workflow.execute_activity("match_subject", payload,
                                            start_to_close_timeout=timedelta(minutes=10),
                                            retry_policy=GRADE_RETRY)
            self._graded += 1
        except Exception as error:
            self._failed += 1
            code = _error_code(error)
            try:
                await workflow.execute_activity(
                    "record_match_failure", {**payload, "errorCode": code},
                    start_to_close_timeout=timedelta(minutes=1), retry_policy=DEFAULT_RETRY)
            except Exception:
                # Even the record failed. The count still tells the truth, which is what the summary shows.
                self._notes.append(f"subject {subject_id} failed and could not be recorded")

    async def _project(self, request: BulkMatchInput, status: str, *,
                       result_summary: Optional[dict] = None, error_code: str = "",
                       error_summary: str = "") -> None:
        """Report this run's lifecycle to `workflow_runs`, without ever failing the run over it."""
        payload: dict[str, Any] = {"clientId": request.client_id, "status": status,
                                   "workflowId": workflow.info().workflow_id}
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
            self._notes.append("run status could not be recorded")

    @workflow.query(name="stage")
    def stage(self) -> str:
        """Progress without touching the database - what a polling UI reads."""
        return self._stage

    @workflow.query(name="progress")
    def progress(self) -> dict:
        """How far through: selected, graded, failed. The numbers a bulk run is watched by."""
        return {"stage": self._stage, "selected": self._selected, "graded": self._graded,
                "failed": self._failed}
