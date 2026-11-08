"""Running a workflow worker: one task queue, its workflows and activities, with bounded concurrency.

A worker is a long-lived process that polls one task queue. Each service owns the workers for the work it
owns (the onboarding worker, the skill-run worker), and KEDA scales each on its own task-queue backlog, which
is why concurrency and queue names come from settings rather than from code.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from agent_common.logging import get_logger, log_event

log = get_logger(__name__)


@dataclass(frozen=True)
class WorkerSpec:
    """What one worker runs.

    `max_concurrent_activities` is the real throttle: activities call LLMs and MCP services, so this is what
    stops one replica stampeding a model gateway. It comes from settings so it can be tuned per environment
    without a release.
    """

    task_queue: str
    workflows: Sequence[type] = field(default_factory=tuple)
    activities: Sequence[Callable[..., Any]] = field(default_factory=tuple)
    max_concurrent_activities: int = 4
    max_concurrent_workflow_tasks: int = 32
    graceful_shutdown_s: int = 30

    def validate(self) -> None:
        """Fail at startup rather than silently polling a queue with nothing registered."""
        if not self.task_queue:
            raise ValueError("a worker needs a task queue")
        if not self.workflows and not self.activities:
            raise ValueError(f"worker for '{self.task_queue}' registers no workflows or activities")
        if self.max_concurrent_activities < 1:
            raise ValueError("max_concurrent_activities must be at least 1")


async def run_worker(client, spec: WorkerSpec, *, stop: Optional[asyncio.Event] = None) -> None:
    """Run until cancelled or until `stop` is set.

    Shutdown is graceful: the worker stops polling, lets running activities finish within
    `graceful_shutdown_s`, and cancels whatever is still going - so a rolling deploy does not abandon work
    mid-flight, and a cancel still propagates to the LLM call underneath (CLAUDE.md section 6).
    """
    spec.validate()
    from datetime import timedelta

    from temporalio.worker import Worker

    worker = Worker(
        client,
        task_queue=spec.task_queue,
        workflows=list(spec.workflows),
        activities=list(spec.activities),
        max_concurrent_activities=spec.max_concurrent_activities,
        max_concurrent_workflow_tasks=spec.max_concurrent_workflow_tasks,
        graceful_shutdown_timeout=timedelta(seconds=spec.graceful_shutdown_s),
    )
    log_event(log, "worker.starting", task_queue=spec.task_queue, workflows=len(spec.workflows),
              activities=len(spec.activities), max_concurrent_activities=spec.max_concurrent_activities)
    try:
        if stop is None:
            await worker.run()
        else:
            async with worker:
                await stop.wait()
    except asyncio.CancelledError:
        log_event(log, "worker.cancelled", task_queue=spec.task_queue)
        raise                       # never swallow cancellation: the process is shutting down
    finally:
        log_event(log, "worker.stopped", task_queue=spec.task_queue)
