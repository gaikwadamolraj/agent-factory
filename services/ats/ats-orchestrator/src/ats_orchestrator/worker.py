"""Onboarding worker entry point: `python -m ats_orchestrator.worker`.

A separate process from the API, in the same image and on the same code, polling the `onboarding` task queue.
Separate because the two scale on completely different signals: the API scales on request rate, this scales on
the queue's backlog (KEDA), and a burst of a thousand resumes must not make the chat slower.

It reuses the service's own container, so an activity reaches the same database, guardrail and knowledge graph
the API does - with the same tenancy, resilience and token handling - rather than a second, drifting wiring.

Shutdown is graceful on SIGTERM: stop polling, let running activities finish inside
`temporal_graceful_shutdown_s`, then cancel what is left so a rolling deploy neither abandons work nor keeps
an LLM stream running (CLAUDE.md section 6).
"""
import asyncio
import signal

from agent_common.logging import get_logger, log_event
from agent_common.telemetry import setup_observability
from agent_common.workflow import WorkerSpec, WorkflowClient, run_worker
from agent_common.workflow.projection import RunProjection

from ats_orchestrator.config import OrchestratorSettings, get_settings
from ats_orchestrator.container import close_container, get_container, init_container
from ats_orchestrator.errors import OrchErrors
from ats_orchestrator.workflows import workflow_classes
from ats_orchestrator.workflows.activities import OnboardActivities
from ats_orchestrator.workflows.adapters import build_onboard_dependencies
from ats_orchestrator.workflows.bulk_activities import BulkMatchActivities
from ats_orchestrator.workflows.bulk_adapters import build_bulk_dependencies

log = get_logger("ats_orchestrator.worker")
_ = OrchErrors  # register service error codes at import, exactly as the API does


def _stop_event() -> asyncio.Event:
    """An event set by SIGTERM/SIGINT, so Kubernetes' stop signal becomes a graceful drain.

    `add_signal_handler` is not implemented on Windows; there the worker is stopped with Ctrl+C, which raises
    KeyboardInterrupt and is handled by the caller.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        handled = getattr(signal, signal_name, None)
        if handled is None:
            continue
        try:
            loop.add_signal_handler(handled, stop.set)
        except NotImplementedError:
            pass
    return stop


def queue_activities(settings: OrchestratorSettings, container, task_queue: str) -> list:
    """The activities to register for one task queue.

    Per queue rather than all of them, because the two journeys need different dependencies: onboarding
    needs the guardrail and the knowledge graph, bulk match needs the screening agent. Registering only what
    a queue runs also means a misrouted run fails loudly with an unknown activity instead of quietly doing
    something the deployment never intended.
    """
    if task_queue == settings.bulk_match_task_queue:
        return BulkMatchActivities(container.db,
                                   **build_bulk_dependencies(container.db, settings, container.agent)).all()
    return OnboardActivities(container.db,
                             **build_onboard_dependencies(container.db, settings, container.guardrails,
                                                          container.knowledge)).all()


def build_spec(settings: OrchestratorSettings, container) -> WorkerSpec:
    """What this worker registers: every journey's workflows, its own queue's activities, and the projection.

    The projection activity is registered on every queue that runs a journey, because every journey reports
    its own lifecycle to `workflow_runs` (agent_common.workflow.projection).
    """
    activities = queue_activities(settings, container, settings.worker_queue)
    return WorkerSpec(task_queue=settings.worker_queue,
                      workflows=workflow_classes(),
                      activities=[*activities, *RunProjection(container.db).activities()],
                      max_concurrent_activities=settings.temporal_max_concurrent_activities,
                      max_concurrent_workflow_tasks=settings.temporal_max_concurrent_workflow_tasks,
                      graceful_shutdown_s=settings.temporal_graceful_shutdown_s)


async def main() -> None:
    """Start the container, connect to the engine and poll until stopped.

    Exits with a clear error rather than idling if no engine is configured: a worker deployment with an empty
    `TEMPORAL_ADDRESS` is a misconfiguration, and a pod that silently does nothing is the worst way to find out.
    """
    settings = get_settings()
    observability = setup_observability(settings)
    if not settings.temporal_address:
        raise SystemExit("TEMPORAL_ADDRESS is not set: this worker has no engine to poll.")
    await init_container(settings)
    client = WorkflowClient(settings.temporal_address, settings.temporal_namespace,
                            identity=f"{settings.service_name}-{settings.worker_queue}",
                            connect_timeout_s=settings.temporal_connect_timeout_s,
                            call_timeout_s=settings.temporal_call_timeout_s)
    try:
        connection = await client.connect()
        spec = build_spec(settings, get_container())
        log_event(log, "worker.ready", env=settings.app_env, task_queue=spec.task_queue,
                  namespace=settings.temporal_namespace)
        await run_worker(connection, spec, stop=_stop_event())
    finally:
        await client.close()
        await close_container()
        observability.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
