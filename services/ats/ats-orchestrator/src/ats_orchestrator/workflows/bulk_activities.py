"""Activities of the bulk-match journey: selecting subjects, grading one, recording a failure, ranking.

Plain async methods on a class holding its dependencies, so every one can be called and asserted on without
Temporal running (CLAUDE.md section 2). The `@activity.defn` decorators only name them for the worker.

Two properties the workflow depends on:

* **Grading one subject is idempotent.** The result row is keyed on `(run, subject)`, so a retry after a
  worker crash updates it instead of paying for a second LLM call.
* **Cancellation propagates.** Nothing here swallows `asyncio.CancelledError`: aborting a thousand-subject
  run must stop the spend immediately, not at the end of the batch.
"""
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agent_common.db import MatchResult, MatchResultRepository, SubjectRepository, WorkflowRunRepository
from agent_common.logging import get_logger, log_event

log = get_logger(__name__)

# Carried out of an activity on `ApplicationError.type` so the row records the real reason.
SUBJECT_UNAVAILABLE = "SCR-NFD-0001"
CRITERIA_UNAVAILABLE = "SCR-MIS-0003"


def _as_dict(value: Any) -> dict:
    """Activity arguments arrive as dataclasses or plain dicts depending on the caller; normalise once."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return dict(value or {})


class BulkMatchActivities:
    """Dependencies in, activities out.

    `grader` is the only domain-specific piece: given a subject and a criteria reference it returns a graded
    result. Recruitment passes a resume-vs-JD screener; insurance will pass a claim-vs-policy one, and
    nothing else in this file changes.
    """

    def __init__(self, database, grader=None, run_lookup=None) -> None:
        self._database = database
        self._grader = grader
        # How an activity turns the workflow id it is running under into the `workflow_runs` row id that
        # owns these results. Injected so the activities stay testable without Temporal.
        self._run_lookup = run_lookup

    # --------------------------------------------------------------- select
    @activity.defn(name="select_subjects")
    async def select_subjects(self, payload: Any) -> dict:
        """One page of this project's subjects of a type, oldest id first. Returns ids and a cursor.

        Paged by id rather than by offset: a bulk run can take an hour, and OFFSET over a table someone is
        still inserting into silently skips and repeats rows.
        """
        data = _as_dict(payload)
        client_id, project_id = int(data["clientId"]), int(data["projectId"])
        limit = max(1, min(int(data.get("limit", 50)), 500))
        cursor = data.get("cursor")
        async with self._database.tenant_session(client_id) as session:
            subjects = SubjectRepository(session)
            rows = await subjects.page_for_type(client_id, project_id, data["domain"], data["subjectType"],
                                                limit=limit, after_id=int(cursor) if cursor else None)
            ids = [int(row.id) for row in rows]
        next_cursor = ids[-1] if len(ids) == limit else None
        log_event(log, "bulk.selected", count=len(ids), subject_type=data["subjectType"],
                  has_more=next_cursor is not None)
        return {"subjectIds": ids, "nextCursor": next_cursor}

    # ---------------------------------------------------------------- grade
    @activity.defn(name="match_subject")
    async def match_subject(self, payload: Any) -> dict:
        """Grade one subject against the criteria set and store the result. Returns a small summary.

        Raises a non-retryable `ApplicationError` when the subject or the criteria cannot be read by this
        client and project: retrying cannot fix a missing document, and a run must not spend an hour
        rediscovering that. Anything transient propagates so Temporal retries it.
        """
        data = _as_dict(payload)
        client_id, project_id = int(data["clientId"]), int(data["projectId"])
        subject_id = int(data["subjectId"])
        if self._grader is None:
            raise ApplicationError("no grader is configured for this domain", type=CRITERIA_UNAVAILABLE,
                                   non_retryable=True)

        async with self._database.tenant_session(client_id) as session:
            subject = await SubjectRepository(session).get(subject_id)
            missing = subject is None or subject.client_id != client_id or subject.project_id != project_id
            graph_ref = "" if missing else subject.graph_ref
        if missing:
            raise ApplicationError(f"subject {subject_id} is not available in this project",
                                   type=SUBJECT_UNAVAILABLE, non_retryable=True)

        try:
            graded = await self._grader.grade(client_id=client_id, project_id=project_id,
                                              subject_id=subject_id, criteria_ref=data["criteriaRef"],
                                              guidance_version=data.get("guidanceVersion", ""))
        except ValueError as error:
            # The grader's way of saying "this cannot be graded as asked": a missing job description, a
            # candidate with no readable resume, a malformed reference. Retrying cannot fix any of them, and
            # three more attempts per subject across a thousand-subject run is an hour spent learning
            # nothing. Recorded with its code and the run moves on.
            raise ApplicationError(str(error)[:200], type=CRITERIA_UNAVAILABLE, non_retryable=True) from None
        run_id = await self._run_id(client_id)
        async with self._database.tenant_session(client_id) as session:
            await MatchResultRepository(session).upsert(MatchResult(
                client_id=client_id, project_id=project_id, run_id=run_id, subject_id=subject_id,
                domain=data.get("domain", ""), criteria_key=data.get("criteriaKey", "")[:64],
                criteria_ref=str(data.get("criteriaRef", ""))[:128],
                rubric_key=str(graded.get("rubricKey", ""))[:64],
                rubric_version=str(graded.get("rubricVersion", ""))[:32],
                score=_decimal(graded.get("score")), band=str(graded.get("band", ""))[:64],
                recommendation=str(graded.get("recommendation", ""))[:128],
                grades=dict(graded.get("grades", {})), breakdown=dict(graded.get("breakdown", {}))))
        log_event(log, "bulk.graded", subject_id=subject_id, score=graded.get("score"),
                  band=graded.get("band"))
        return {"subjectId": subject_id, "subjectRef": graph_ref, "score": graded.get("score"),
                "band": graded.get("band")}

    # -------------------------------------------------------------- failure
    @activity.defn(name="record_match_failure")
    async def record_match_failure(self, payload: Any) -> dict:
        """Record that one subject could not be graded, so the run's counts tell the truth.

        A silently skipped subject is indistinguishable from one found unsuitable, and in a shortlist that
        difference is the whole answer.
        """
        data = _as_dict(payload)
        client_id, project_id = int(data["clientId"]), int(data["projectId"])
        subject_id = int(data["subjectId"])
        run_id = await self._run_id(client_id)
        async with self._database.tenant_session(client_id) as session:
            await MatchResultRepository(session).upsert(MatchResult(
                client_id=client_id, project_id=project_id, run_id=run_id, subject_id=subject_id,
                domain=data.get("domain", ""), criteria_key=str(data.get("criteriaKey", ""))[:64],
                criteria_ref=str(data.get("criteriaRef", ""))[:128], score=None,
                error_code=str(data.get("errorCode", ""))[:32] or None,
                error_summary="the subject could not be graded in this run"))
        log_event(log, "bulk.grade_failed", subject_id=subject_id, code=data.get("errorCode"))
        return {"subjectId": subject_id, "recorded": True}

    # ----------------------------------------------------------------- rank
    @activity.defn(name="rank_results")
    async def rank_results(self, payload: Any) -> dict:
        """Number this run's results by score and return the top N as a small shortlist.

        The shortlist is references and numbers only - a subject ref, a score, a band - because it goes into
        workflow history and the run's summary. The full ranking stays in `match_results`.
        """
        data = _as_dict(payload)
        client_id, project_id = int(data["clientId"]), int(data["projectId"])
        top_n = max(1, min(int(data.get("topN", 50)), 500))
        run_id = await self._run_id(client_id)
        async with self._database.tenant_session(client_id) as session:
            results = MatchResultRepository(session)
            ranked_rows = await results.assign_ranks(client_id, run_id)
            counts = await results.counts(client_id, run_id)
            top = [row for row in await results.ranked(client_id, run_id, limit=top_n)
                   if not row.error_code]       # a failure never occupies a place in a shortlist
            # One query for every subject on the shortlist, not one per row: a top-500 shortlist built in a
            # loop is five hundred round trips at the end of a run that is already long.
            subjects = await SubjectRepository(session).by_ids(client_id, project_id,
                                                               [int(row.subject_id) for row in top])
            shortlist = []
            for row in top:
                subject = subjects.get(int(row.subject_id))
                shortlist.append({"subjectId": int(row.subject_id),
                                  "subjectRef": subject.graph_ref if subject else "",
                                  "displayName": (subject.display_name if subject else "")[:255],
                                  "score": float(row.score) if row.score is not None else None,
                                  "band": row.band, "rank": row.rank})
        log_event(log, "bulk.ranked", run_id=run_id, ranked=ranked_rows, shortlist=len(shortlist),
                  graded=counts["graded"], failed=counts["failed"])
        return {"shortlist": shortlist, **counts}

    # --------------------------------------------------------------- shared
    async def _run_id(self, client_id: int) -> int:
        """The `workflow_runs` row id this activity's run belongs to.

        Read from the activity's own Temporal workflow id rather than passed in the payload: the payload is
        built by the workflow and a caller must not be able to write results into another run's rows.
        """
        if self._run_lookup is not None:
            return await self._run_lookup(client_id, activity.info().workflow_id)
        async with self._database.tenant_session(client_id) as session:
            row = await WorkflowRunRepository(session).get_by_workflow_id(client_id,
                                                                          activity.info().workflow_id)
            if row is None:
                raise ApplicationError("this run has no record to attach results to",
                                       type="WFL-NFD-0002", non_retryable=True)
            return int(row.id)

    def all(self) -> list:
        """Everything a worker on the bulk-match queue must register."""
        return [self.select_subjects, self.match_subject, self.record_match_failure, self.rank_results]


def _decimal(value: Any) -> Optional[Decimal]:
    """A score as a decimal, or None. Scores are ordered and shown to people, so they are never floats."""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return None
