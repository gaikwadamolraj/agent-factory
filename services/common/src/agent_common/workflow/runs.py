"""Launching a journey and keeping its business record (`workflow_runs`) in step with the engine.

Temporal is the source of truth for *execution*: history, replay, retries, timers. `workflow_runs` is the
source of truth for the *product*: which runs a project has, what they were about, how they ended, what they
cost. Keeping both means the UI, the API and an audit never depend on the engine being reachable, and a run
survives a Temporal namespace being rebuilt (docs/impl_temporal.md section 3).

Launching is idempotent on two levels, and both are needed:

* **The engine** de-duplicates on the workflow id, which `workflow_id_for` derives from the client and the
  business key. Two concurrent submits produce one execution.
* **The database** de-duplicates on `(client_id, idempotency_key)`. That is what lets a caller discover an
  earlier run's outcome without asking Temporal, and what stops a retry storm writing a row per attempt.

This module is deliberately free of the Temporal SDK so that any service can import it (the activity that
writes completions lives in `projection.py`, which workers import and which does need the SDK).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from agent_common.constants import RUN_RESTARTABLE_STATUSES, RUN_TERMINAL_STATUSES, RunStatus
from agent_common.db import WorkflowRun, WorkflowRunRepository
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.workflow.client import WorkflowClient, WorkflowStartOptions, check_payload, workflow_id_for

log = get_logger(__name__)


@dataclass(frozen=True)
class JourneyRequest:
    """One durable run to launch.

    `argument` is what the workflow receives and `input_ref` is what the row records; they are separate
    because the row is read by people and by other services, so it holds the identifiers worth showing, not
    the workflow's internal shape. Both carry references only - ids and hashes, never document content.
    """

    client_id: int
    project_id: int
    domain: str                        # pack id: ats | insurance | banking | media
    journey: str                       # onboard_subject | match_subject | bulk_match | ...
    workflow: Any                      # the registered workflow name, or its class
    task_queue: str
    idempotency_key: str               # the business key; unique per client
    argument: Any = None
    input_ref: dict[str, Any] = field(default_factory=dict)
    subject_id: Optional[int] = None
    priority_tier: str = "standard"    # derived server-side from the plan, never taken from a client
    execution_timeout_s: Optional[int] = None

    @property
    def workflow_type(self) -> str:
        """The registered workflow name, whether `workflow` was given as a name or as a class."""
        return self.workflow if isinstance(self.workflow, str) else getattr(self.workflow, "__name__", "")


@dataclass(frozen=True)
class LaunchedRun:
    """What the caller needs back: the run's public id and whether any new work was actually started."""

    run_external_id: str
    workflow_id: str
    run_id: str
    status: str
    reattached: bool = False           # True when an equivalent run already existed


class JourneyLauncher:
    """Starts journeys and owns their `workflow_runs` rows.

    One instance per service, built from the shared `Database` and `WorkflowClient`. It is the only place
    that writes the projection on the way in; the way out is written by `projection.RunProjection`.
    """

    def __init__(self, database, client: WorkflowClient) -> None:
        self._database, self._client = database, client

    @property
    def enabled(self) -> bool:
        """False when no engine is configured: the caller runs its non-durable path instead of failing."""
        return self._client.configured

    async def launch(self, request: JourneyRequest) -> LaunchedRun:
        """Start the run, or reattach to the equivalent one that already exists.

        Returns the run's identifiers and `reattached=True` when no new work was started. Raises
        `AppError(SYS-UNA-...)` when the engine is unreachable and `AppError(VAL-INV-...)` when it refuses
        the run (both from `WorkflowClient.start`); the projection row is only written once the engine has
        accepted the run, so a row never claims work that does not exist.
        """
        if not request.idempotency_key:
            raise AppError(Errors.VALIDATION_FAILED,
                           fields=[{"field": "idempotencyKey", "message": "a durable run needs a business key"}])
        check_payload(request.input_ref, what="inputRef")
        workflow_id = workflow_id_for(request.client_id, request.journey, request.idempotency_key)

        existing = await self._existing_run(request.client_id, request.idempotency_key)
        if existing is not None and existing["status"] not in RUN_RESTARTABLE_STATUSES:
            # Already running, waiting, or finished successfully: report it rather than doing the work twice.
            log_event(log, "run.reattached", journey=request.journey, workflow_id=workflow_id,
                      status=existing["status"])
            return LaunchedRun(run_external_id=existing["external_id"], workflow_id=workflow_id,
                               run_id=existing["run_id"], status=existing["status"], reattached=True)

        # Started outside any database transaction: a network call must never hold a row lock open.
        handle = await self._client.start(
            request.workflow, request.argument,
            WorkflowStartOptions(task_queue=request.task_queue, workflow_id=workflow_id,
                                 execution_timeout_s=request.execution_timeout_s))
        return await self._record_start(request, handle.workflow_id, handle.run_id)

    async def _existing_run(self, client_id: int, idempotency_key: str) -> Optional[dict]:
        """The stored run for this business key as plain values, so no ORM object escapes its session."""
        async with self._database.tenant_session(client_id) as session:
            row = await WorkflowRunRepository(session).get_by_idempotency_key(client_id, idempotency_key)
            if row is None:
                return None
            return {"external_id": str(row.external_id), "run_id": row.temporal_run_id, "status": row.status}

    async def _record_start(self, request: JourneyRequest, workflow_id: str, run_id: str) -> LaunchedRun:
        """Insert the row, or update the restartable one this launch replaced.

        A concurrent launcher may insert first; the unique constraint turns that race into an
        `IntegrityError`, which is resolved by reading the winner's row. Both callers then describe the same
        single execution, because the engine de-duplicated on the workflow id as well.
        """
        try:
            async with self._database.tenant_session(request.client_id) as session:
                runs = WorkflowRunRepository(session)
                row = await runs.get_by_idempotency_key(request.client_id, request.idempotency_key)
                if row is None:
                    row = WorkflowRun(client_id=request.client_id, project_id=request.project_id,
                                      domain=request.domain, journey=request.journey,
                                      subject_id=request.subject_id, workflow_type=request.workflow_type,
                                      task_queue=request.task_queue, temporal_workflow_id=workflow_id,
                                      temporal_run_id=run_id, idempotency_key=request.idempotency_key,
                                      priority_tier=request.priority_tier, status=RunStatus.QUEUED.value,
                                      input_ref=dict(request.input_ref))
                    await runs.add(row)
                else:
                    # A failed or cancelled run being retried: same business key, new execution, clean slate.
                    row.temporal_run_id, row.status = run_id, RunStatus.QUEUED.value
                    row.input_ref = dict(request.input_ref)
                    row.error_code = row.error_summary = None
                    row.result_summary, row.started_at, row.completed_at = {}, None, None
                    row.updated_at = datetime.now(timezone.utc)
                await session.flush()
                external_id = str(row.external_id)
        except IntegrityError:
            log_event(log, "run.launch_raced", journey=request.journey, workflow_id=workflow_id)
            winner = await self._existing_run(request.client_id, request.idempotency_key)
            if winner is None:                       # the constraint fired for some other reason: surface it
                raise
            return LaunchedRun(run_external_id=winner["external_id"], workflow_id=workflow_id,
                               run_id=winner["run_id"], status=winner["status"], reattached=True)
        log_event(log, "run.launched", journey=request.journey, domain=request.domain,
                  workflow_id=workflow_id, run_id=run_id, task_queue=request.task_queue)
        return LaunchedRun(run_external_id=external_id, workflow_id=workflow_id, run_id=run_id,
                           status=RunStatus.QUEUED.value)


    async def cancel(self, client_id: int, workflow_id: str, *, reason: str = "") -> bool:
        """Ask the engine to stop a run, and record the cancellation ourselves.

        The run's own code cannot write this: once a workflow is cancelled it may not start ordinary
        activities, so the canceller is the one that owns the projection for this transition. Returns False
        when there is no such run for this client - which is also what stops one tenant cancelling another's
        work, since the lookup is tenant-scoped.

        Cancellation is cooperative end to end: Temporal cancels the workflow, the workflow cancels the
        activity, the activity cancels its MCP call and the LLM stream closes, so the spend stops at once
        (CLAUDE.md section 6).
        """
        async with self._database.tenant_session(client_id) as session:
            runs = WorkflowRunRepository(session)
            row = await runs.get_by_workflow_id(client_id, workflow_id)
            if row is None:
                return False
            already_finished = row.status in RUN_TERMINAL_STATUSES
        if not already_finished:
            await self._client.cancel(workflow_id)
        async with self._database.tenant_session(client_id) as session:
            row = await WorkflowRunRepository(session).get_by_workflow_id(client_id, workflow_id)
            if row is None or row.status in RUN_TERMINAL_STATUSES:
                return False                    # it finished while we were asking: leave its real outcome
            now = datetime.now(timezone.utc)
            row.status, row.completed_at, row.updated_at = RunStatus.CANCELLED.value, now, now
            row.error_summary = (reason or "cancelled by request")[:500]
        log_event(log, "run.cancelled", workflow_id=workflow_id)
        return True
