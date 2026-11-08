"""The activity that keeps `workflow_runs` in step with a run in flight.

`JourneyLauncher` writes the row when a run is accepted; this writes it as the run progresses and ends. It is
one activity rather than several so that every journey registers the same single name on its worker, and so a
new journey gets the projection for free.

**This module imports the Temporal SDK, so only workers import it** - never a workflow definition module,
which the sandbox re-imports on every worker start (docs/impl_temporal.md section 5).

The projection is deliberately best-effort *from the workflow's point of view*: Temporal retries this activity
on a transient database failure, but if it still cannot be written the business run is not failed because of
it. Losing a status update costs accuracy in a dashboard; failing a completed screening because its dashboard
row would not save costs the user their work. The drift is visible - the row stays in its previous status
while the engine shows the run closed - and `agent_common.workflow.client.describe` reads the truth.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from temporalio import activity

from agent_common.constants import RUN_TERMINAL_STATUSES, RunStatus
from agent_common.db import WorkflowRunRepository
from agent_common.logging import get_logger, log_event

log = get_logger(__name__)

RECORD_RUN_STATUS = "record_run_status"      # the registered activity name; workflows refer to it by string

_ALLOWED_STATUSES = frozenset(status.value for status in RunStatus)


class RunProjection:
    """Writes run status changes to `workflow_runs`.

    One instance per worker, holding the shared `Database`. Register `activities()` on every task queue whose
    workflows report progress.
    """

    def __init__(self, database) -> None:
        self._database = database

    @activity.defn(name=RECORD_RUN_STATUS)
    async def record_run_status(self, payload: Any) -> dict:
        """Apply one status change to the run's row.

        Payload (built by `agent_common.workflow.runs.status_update`, plus the ids the workflow supplies):
        `clientId`, `workflowId`, `status`, and optionally `resultSummary`, `subjectId`, `errorCode`,
        `errorSummary`. Returns `{"updated": bool}`; a missing row is reported, not raised, because a run
        started outside the launcher (a replay against a restored database, a manual start from the Temporal
        UI) must still be allowed to finish.

        Side effects: sets `started_at` the first time the run reports `running`, and `completed_at` on any
        terminal status. Raises ValueError on an unknown status, which is a programming error and should fail
        loudly rather than write a status nothing can filter on.
        """
        data = dict(payload or {})
        status = str(data.get("status", ""))
        if status not in _ALLOWED_STATUSES:
            raise ValueError(f"unknown run status '{status}'")
        client_id, workflow_id = int(data["clientId"]), str(data["workflowId"])

        async with self._database.tenant_session(client_id) as session:
            runs = WorkflowRunRepository(session)
            row = await runs.get_by_workflow_id(client_id, workflow_id)
            if row is None:
                log_event(log, "run.projection_missing", workflow_id=workflow_id, status=status)
                return {"updated": False}
            now = datetime.now(timezone.utc)
            row.status = status
            if status == RunStatus.RUNNING.value and row.started_at is None:
                row.started_at = now
            if status in RUN_TERMINAL_STATUSES:
                row.completed_at = now
                if row.started_at is None:
                    row.started_at = now          # finished without ever reporting running (a fast failure)
            summary = data.get("resultSummary")
            if isinstance(summary, dict) and summary:
                row.result_summary = {**(row.result_summary or {}), **summary}
            if data.get("subjectId"):
                row.subject_id = int(data["subjectId"])
            row.error_code = _trim(data.get("errorCode"), 32)
            row.error_summary = _trim(data.get("errorSummary"), 500)
            row.updated_at = now
        log_event(log, "run.projected", workflow_id=workflow_id, status=status)
        return {"updated": True}

    def activities(self) -> list:
        """Everything a worker must register for the projection to be written."""
        return [self.record_run_status]


def _trim(value: Any, limit: int) -> Optional[str]:
    """Keep error fields inside their column widths, and store nothing rather than an empty string."""
    if not value:
        return None
    return str(value)[:limit]
