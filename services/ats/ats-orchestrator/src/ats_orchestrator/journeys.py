"""Producers: the places where this service hands work to a durable journey.

A producer is the one part of a journey that cannot live in `workflows/`. The workflow package describes
*what* the work is; this file decides *when* starting it is worth it and what business key identifies the
run. Keeping the two apart means a second domain reuses the workflow and writes its own producer, and it
keeps this file as the complete list of everything that can start durable work in the orchestrator.

Every producer degrades to the in-process path. With no engine configured (`launcher.enabled` is False) the
functions here return `None` and the caller does what it did before, so a deployment without Temporal behaves
exactly as it used to (docs/impl_temporal.md section 4).
"""
from typing import Optional

from agent_common.db.repositories import TenantScope
from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from agent_common.workflow import JourneyRequest, LaunchedRun

from ats_orchestrator.container import get_container
from ats_orchestrator.knowledge import resume_source_ref
from ats_orchestrator.workflows import (BULK_MATCH_JOURNEY, ONBOARD_JOURNEY, BulkMatchInput, OnboardInput)

log = get_logger(__name__)

# The ATS pack's subject type. Other packs onboard a `claim`, a `customer`, a `brief`; the journey itself
# does not care, which is the point of docs/impl_multi_domain.md section 5.
CANDIDATE_SUBJECT_TYPE = "candidate"


def candidate_external_ref(document_id: int) -> str:
    """The candidate's identity until a pack extracts a better one: the resume that describes them.

    This is deliberately the same string the knowledge graph uses as its `subject_ref`
    (`agent_common`-side: `knowledge.resume_source_ref`), so the `subjects` row and the graph node describe
    one person rather than two records that happen to share a document.
    """
    return resume_source_ref(document_id)


async def start_candidate_onboarding(scope: TenantScope, document_id: int, *,
                                     display_name: str = "") -> Optional[LaunchedRun]:
    """Start (or reattach to) the durable onboarding of the candidate described by one resume.

    Returns the launched run, or `None` when there is no engine configured or the launch failed - in both
    cases the caller must fall back to its in-process path, because an upload must not fail over the engine
    being unavailable. The business key is the resume, so re-uploading the same file, retrying the request or
    two users submitting at once all reattach to the one run (`JourneyLauncher.launch`).

    Side effects: one `workflow_runs` row per resume, and the onboarding pipeline itself - scan, subject
    record, re-index, knowledge graph.
    """
    container = get_container()
    if not container.launcher.enabled:
        return None

    settings = container.settings
    external_ref = candidate_external_ref(document_id)
    argument = OnboardInput(client_id=scope.client_id, project_id=scope.project_id, domain=settings.pack_domain,
                            subject_type=CANDIDATE_SUBJECT_TYPE, document_id=document_id,
                            external_ref=external_ref, display_name=display_name[:255],
                            guidance_version=settings.knowledge_guidance_version)
    request = JourneyRequest(
        client_id=scope.client_id, project_id=scope.project_id, domain=settings.pack_domain,
        journey=ONBOARD_JOURNEY, workflow=ONBOARD_JOURNEY, task_queue=settings.onboarding_task_queue,
        idempotency_key=f"{settings.pack_domain}:{CANDIDATE_SUBJECT_TYPE}:{external_ref}",
        argument=argument,
        input_ref={"documentId": document_id, "subjectRef": f"{CANDIDATE_SUBJECT_TYPE}:{external_ref}"},
        execution_timeout_s=settings.onboarding_timeout_s)
    try:
        launched = await container.launcher.launch(request)
    except AppError as error:
        # The engine is unreachable or refused the run. The upload has already succeeded and the document is
        # stored and indexed; degrading to the in-process path is strictly better than failing the turn.
        log_event(log, "onboard.launch_failed", document_id=document_id, code=error.spec.code)
        return None
    log_event(log, "onboard.launched", document_id=document_id, run_id=launched.run_external_id,
              reattached=launched.reattached, status=launched.status)
    return launched


async def start_bulk_match(scope: TenantScope, jd_document_id: int, *,
                           top_n: Optional[int] = None,
                           max_subjects: Optional[int] = None) -> Optional[LaunchedRun]:
    """Start (or reattach to) a bulk match of every candidate in the project against one job description.

    Returns the launched run, or `None` when there is no engine configured or the launch failed. Unlike
    onboarding there is no in-process fallback: grading a thousand candidates inside a request is not a
    degraded version of this journey, it is a different and much worse thing, so the caller must tell the
    user that bulk matching is unavailable rather than quietly attempting it.

    The business key is the job description, so submitting the same shortlist twice reattaches to the one
    run instead of paying for a thousand screenings again.

    Side effects: one `workflow_runs` row, one `match_results` row per candidate graded, and up to
    `max_subjects` screening calls.
    """
    container = get_container()
    if not container.launcher.enabled:
        return None

    settings = container.settings
    criteria_ref = resume_source_ref(jd_document_id)      # the same `document:<id>` vocabulary as elsewhere
    argument = BulkMatchInput(
        client_id=scope.client_id, project_id=scope.project_id, domain=settings.pack_domain,
        subject_type=CANDIDATE_SUBJECT_TYPE, criteria_ref=criteria_ref, criteria_key="job_requirements",
        guidance_version=settings.knowledge_guidance_version,
        top_n=int(top_n or settings.bulk_match_top_n),
        max_subjects=int(max_subjects or settings.bulk_match_max_subjects),
        page_size=settings.bulk_match_page_size, concurrency=settings.bulk_match_concurrency)
    request = JourneyRequest(
        client_id=scope.client_id, project_id=scope.project_id, domain=settings.pack_domain,
        journey=BULK_MATCH_JOURNEY, workflow=BULK_MATCH_JOURNEY,
        task_queue=settings.bulk_match_task_queue,
        idempotency_key=f"{settings.pack_domain}:bulk:{criteria_ref}",
        argument=argument,
        input_ref={"criteriaRef": criteria_ref, "subjectType": CANDIDATE_SUBJECT_TYPE,
                   "maxSubjects": argument.max_subjects},
        # Bulk work is explicitly the lower tier: it is long, it is not waited on, and it must yield to the
        # interactive journeys rather than compete with them.
        priority_tier="bulk",
        execution_timeout_s=settings.bulk_match_timeout_s)
    try:
        launched = await container.launcher.launch(request)
    except AppError as error:
        log_event(log, "bulk.launch_failed", document_id=jd_document_id, code=error.spec.code)
        return None
    log_event(log, "bulk.launched", document_id=jd_document_id, run_id=launched.run_external_id,
              reattached=launched.reattached, max_subjects=argument.max_subjects)
    return launched
