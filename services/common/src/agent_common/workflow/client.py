"""Starting, signalling and querying durable runs.

Everything here is tenant-aware: a workflow id always carries the client, so two tenants can use the same
business key without colliding, and a run is identifiable from its id alone in the Temporal UI.
"""
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.resilience import CircuitOpenError, get_breaker, resilient_call

log = get_logger(__name__)

# The engine is an outbound dependency like any other (CLAUDE.md section 6), and since a user-facing upload
# now starts a journey, an unreachable Temporal must fail fast rather than hold a request open: one named
# breaker per process, explicit timeouts, no retries here (the caller decides what a failed launch means).
BREAKER_NAME = "temporal"
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_CALL_TIMEOUT_S = 15.0

# Temporal stores every argument and result in workflow history, which is replayed on every worker restart.
# Large payloads make replay slow and history limits a production incident, so callers pass references.
# 64 KiB is generous for ids, hashes and small metadata, and far below Temporal's own limits.
MAX_PAYLOAD_BYTES = 64 * 1024


def workflow_id_for(client_id: int, kind: str, key: str) -> str:
    """A stable, tenant-scoped workflow id: `<kind>-c<client>-<key>`.

    Stable because Temporal de-duplicates on it: starting the same onboarding twice reattaches to that run -
    whether it is still going or already finished - instead of doing the work twice (see `start`). `key` is the
    caller's business key, such as a document id or a run id.
    """
    safe_key = "".join(char if char.isalnum() or char in "-_." else "-" for char in key)[:128]
    return f"{kind}-c{int(client_id)}-{safe_key}"


def check_payload(value: Any, *, what: str = "payload") -> Any:
    """Refuse anything too large to belong in workflow history.

    Raises AppError(VAL-INV-0001) naming the field, because the fix is always the same: store the content and
    pass a reference to it.
    """
    try:
        size = len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise AppError(Errors.VALIDATION_FAILED,
                       fields=[{"field": what, "message": f"not serialisable for a workflow: {error}"}]) from None
    if size > MAX_PAYLOAD_BYTES:
        raise AppError(Errors.VALIDATION_FAILED, fields=[{
            "field": what,
            "message": (f"{size} bytes is too large for workflow history (limit {MAX_PAYLOAD_BYTES}); "
                        "store the content and pass a reference")}])
    return value


@dataclass(frozen=True)
class WorkflowStartOptions:
    """How one run is started. Defaults are deliberately conservative."""

    task_queue: str
    workflow_id: str
    execution_timeout_s: Optional[int] = None
    run_timeout_s: Optional[int] = None
    task_timeout_s: int = 30
    search_attributes: dict[str, Any] = field(default_factory=dict)
    memo: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowHandleInfo:
    """What a caller needs to record about a started run (the `workflow_runs` projection)."""

    workflow_id: str
    run_id: str
    task_queue: str


class WorkflowClient:
    """Connects to Temporal and starts/signals/queries runs.

    Connection is lazy and cached: constructing this is cheap and safe at import time, while a Temporal outage
    surfaces on first use as a retryable AppError rather than preventing a service from starting.
    """

    def __init__(self, address: str, namespace: str, *, identity: str = "",
                 connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
                 call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S) -> None:
        self._address, self._namespace, self._identity = address, namespace, identity
        self._connect_timeout_s, self._call_timeout_s = connect_timeout_s, call_timeout_s
        self._breaker = get_breaker(BREAKER_NAME)
        self._client = None

    @property
    def configured(self) -> bool:
        """False when no address is set: the caller should run its non-durable path."""
        return bool(self._address)

    async def connect(self):
        """The underlying SDK client, connected once and reused."""
        if self._client is not None:
            return self._client
        if not self._address:
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, detail="No workflow engine is configured.")
        client_class = _client_class()
        try:
            self._client = await resilient_call(
                lambda: client_class.connect(self._address, namespace=self._namespace,
                                             identity=self._identity or None),
                op="temporal.connect", timeout_s=self._connect_timeout_s, breaker=self._breaker)
        except CircuitOpenError as error:
            # The engine has been failing: refuse immediately instead of making every caller wait for the
            # same timeout again.
            log_event(log, "workflow.connect_circuit_open", namespace=self._namespace)
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=error.retry_after_s) from None
        except Exception as error:
            log_event(log, "workflow.connect_failed", address=self._address, namespace=self._namespace,
                      error_type=type(error).__name__)
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=5) from None
        log_event(log, "workflow.connected", namespace=self._namespace)
        return self._client

    async def start(self, workflow: Any, argument: Any, options: WorkflowStartOptions) -> WorkflowHandleInfo:
        """Start a run (or reattach to the one that already has this id) and return its identifiers."""
        check_payload(argument, what="workflow argument")
        client = await self.connect()
        from temporalio.common import WorkflowIDReusePolicy

        kwargs: dict[str, Any] = {
            "id": options.workflow_id,
            "task_queue": options.task_queue,
            "id_reuse_policy": WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        }
        if options.execution_timeout_s:
            kwargs["execution_timeout"] = _seconds(options.execution_timeout_s)
        if options.run_timeout_s:
            kwargs["run_timeout"] = _seconds(options.run_timeout_s)
        if options.task_timeout_s:
            kwargs["task_timeout"] = _seconds(options.task_timeout_s)
        if options.search_attributes:
            kwargs["search_attributes"] = options.search_attributes
        if options.memo:
            kwargs["memo"] = options.memo
        from temporalio.service import RPCError

        already_started = _already_started_error()
        try:
            # A duplicate id is the idempotency path working, not an outage: `is_failure` keeps it off the
            # breaker, so a hot business key cannot open the circuit for everyone else.
            handle = await resilient_call(
                lambda: client.start_workflow(workflow, argument, **kwargs),
                op="temporal.start", timeout_s=self._call_timeout_s, breaker=self._breaker,
                is_failure=lambda error: not isinstance(error, already_started))
        except CircuitOpenError as error:
            log_event(log, "workflow.start_circuit_open", workflow_id=options.workflow_id)
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=error.retry_after_s) from None
        except already_started as error:
            # The id is already taken: this is the point of a stable, business-keyed id. Re-submitting the
            # same onboarding reattaches to that run (or reports the finished one) instead of doing the work
            # twice - so a retrying caller is idempotent rather than duplicating side effects.
            existing = getattr(error, "run_id", "") or ""
            log_event(log, "workflow.already_started", workflow_id=options.workflow_id, run_id=existing)
            return WorkflowHandleInfo(workflow_id=options.workflow_id, run_id=existing,
                                      task_queue=options.task_queue)
        except (RPCError, OSError, asyncio.TimeoutError) as error:
            log_event(log, "workflow.start_failed", workflow_id=options.workflow_id,
                      task_queue=options.task_queue, error_type=type(error).__name__)
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=5) from None
        except Exception as error:
            # A bad argument or an unregistered workflow is a caller mistake, not an outage: say so, so it is
            # not retried forever behind a misleading 503.
            log_event(log, "workflow.start_rejected", workflow_id=options.workflow_id,
                      task_queue=options.task_queue, error_type=type(error).__name__)
            raise AppError(Errors.VALIDATION_FAILED, fields=[{
                "field": "workflow", "message": f"the workflow engine refused this run ({type(error).__name__})"}
            ]) from None
        log_event(log, "workflow.started", workflow_id=handle.id, run_id=handle.result_run_id,
                  task_queue=options.task_queue)
        return WorkflowHandleInfo(workflow_id=handle.id, run_id=handle.result_run_id or "",
                                  task_queue=options.task_queue)

    async def _rpc(self, call, *, op: str):
        """One short engine RPC under the shared timeout and breaker.

        Short means "returns as fast as the engine can answer": starting, signalling, cancelling, querying,
        describing. `result` is excluded on purpose - it waits for the run, which can take hours.
        """
        try:
            return await resilient_call(call, op=op, timeout_s=self._call_timeout_s, breaker=self._breaker)
        except CircuitOpenError as error:
            log_event(log, "workflow.circuit_open", op=op)
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=error.retry_after_s) from None
        except AppError:
            raise
        except Exception as error:
            log_event(log, "workflow.rpc_failed", op=op, error_type=type(error).__name__)
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=5) from None

    async def signal(self, workflow_id: str, signal: str, argument: Any = None) -> None:
        """Send a signal (an approval, new evidence, a cancel request) to a running workflow."""
        if argument is not None:
            check_payload(argument, what="signal argument")
        handle = (await self.connect()).get_workflow_handle(workflow_id)
        await self._rpc(lambda: handle.signal(signal, argument) if argument is not None else handle.signal(signal),
                        op="temporal.signal")
        log_event(log, "workflow.signalled", workflow_id=workflow_id, signal=signal)

    async def cancel(self, workflow_id: str) -> None:
        """Request cancellation. The run stops cooperatively, so activities can release what they hold."""
        handle = (await self.connect()).get_workflow_handle(workflow_id)
        await self._rpc(handle.cancel, op="temporal.cancel")
        log_event(log, "workflow.cancelled", workflow_id=workflow_id)

    async def query(self, workflow_id: str, query: str, argument: Any = None) -> Any:
        """Read a running workflow's own view of its progress, without touching the database."""
        handle = (await self.connect()).get_workflow_handle(workflow_id)
        return await self._rpc(
            lambda: handle.query(query, argument) if argument is not None else handle.query(query),
            op="temporal.query")

    async def result(self, workflow_id: str, run_id: str = "") -> Any:
        """Wait for a run's result. Callers that must not block use the `workflow_runs` projection instead.

        Deliberately not under the call timeout: this waits for the business work to finish, which is
        minutes to hours, and cutting it short would report a healthy run as unavailable.
        """
        handle = (await self.connect()).get_workflow_handle(workflow_id, run_id=run_id or None)
        return await handle.result()

    async def describe(self, workflow_id: str) -> dict[str, Any]:
        """Status of a run, as small, JSON-safe values for an API response."""
        handle = (await self.connect()).get_workflow_handle(workflow_id)
        description = await self._rpc(handle.describe, op="temporal.describe")
        return {"workflowId": description.id, "runId": description.run_id,
                "status": description.status.name if description.status is not None else "UNKNOWN",
                "taskQueue": description.task_queue,
                "startedAt": description.start_time.isoformat() if description.start_time else None,
                "closedAt": description.close_time.isoformat() if description.close_time else None}

    async def close(self) -> None:
        """Release the connection (service shutdown)."""
        self._client = None


def _client_class():
    """Import the SDK lazily, with a message that names the missing extra."""
    try:
        from temporalio.client import Client
    except ImportError as error:
        raise AppError(Errors.INTERNAL,
                       detail="The workflow engine client is not installed (agent-common[temporal]).") from error
    return Client


def _seconds(value: int):
    from datetime import timedelta
    return timedelta(seconds=int(value))


def _already_started_error() -> type[BaseException]:
    """The SDK's "this workflow id is already in use" error, looked up lazily (the SDK is optional).

    It lives in `temporalio.exceptions`, not `temporalio.client` - verified against SDK 1.35. Re-check on an
    SDK upgrade: getting it wrong turns an idempotent re-submit into a confusing failure.
    """
    from temporalio.exceptions import WorkflowAlreadyStartedError
    return WorkflowAlreadyStartedError
