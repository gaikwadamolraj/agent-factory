"""Durable workflows owned by the ATS product.

Workflow definitions live in `onboard_subject` and `bulk_match`, their activities beside them. Keep the
split: the Temporal sandbox re-imports a workflow's module on every worker start, so that module must import
only deterministic things - no clients, no settings, no network (docs/impl_temporal.md section 5).

Two journeys, two task queues, and that separation is deliberate. A thousand-candidate bulk match must never
be able to delay one recruiter's upload, and queues are the only boundary that actually guarantees it: a
shared queue with priorities still shares workers, and the bulk run holds every slot for an hour.
"""
from ats_orchestrator.workflows.bulk_match import BulkMatch, BulkMatchInput, BulkMatchResult
from ats_orchestrator.workflows.onboard_subject import (STAGES, OnboardInput, OnboardResult, OnboardSubject)

ONBOARDING_TASK_QUEUE = "onboarding"      # KEDA scales this queue on its own backlog
ONBOARD_JOURNEY = "onboard_subject"       # the journey name recorded on workflow_runs

BULK_MATCH_TASK_QUEUE = "bulk-match"      # its own queue and its own workers: see the note above
BULK_MATCH_JOURNEY = "bulk_match"


def workflow_classes() -> list[type]:
    """Everything a worker must register. One list, so a new journey cannot be half-deployed.

    Registering both on every worker is cheap - a workflow definition is code, not a running thing - and it
    means a worker polling either queue can replay either history. What keeps the work apart is the queue a
    run is *started* on, which is the producer's decision.
    """
    return [OnboardSubject, BulkMatch]


__all__ = ["BULK_MATCH_JOURNEY", "BULK_MATCH_TASK_QUEUE", "ONBOARDING_TASK_QUEUE", "ONBOARD_JOURNEY",
           "STAGES", "BulkMatch", "BulkMatchInput", "BulkMatchResult", "OnboardInput", "OnboardResult",
           "OnboardSubject", "workflow_classes"]
