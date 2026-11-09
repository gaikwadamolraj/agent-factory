"""/api/v1/runs - the durable runs of the selected project, read from the `workflow_runs` projection.

This never talks to the workflow engine. The projection exists so the product can list, show and audit runs
without the engine being reachable (docs/impl_temporal.md section 3), and reading it that way is what keeps
that promise true. Cancelling is the one exception, because stopping work needs the engine.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.constants import RUN_TERMINAL_STATUSES, RunStatus
from agent_common.db import (MatchResultRepository, SubjectRepository, WorkflowRun, WorkflowRunRepository,
                             get_session)
from agent_common.db.repositories import TenantScope
from agent_common.errors import AppError, Errors
from agent_common.security import UserClaims

from ats_orchestrator.api.deps import current_user, tenant_scope
from ats_orchestrator.api.v1.schemas import (BulkMatchRequest, BulkMatchStartedOut, MatchResultOut,
                                             RunCancelOut, RunOut)
from ats_orchestrator.container import get_container
from ats_orchestrator.errors import OrchErrors
from ats_orchestrator.journeys import start_bulk_match

router = APIRouter(prefix="/runs", tags=["runs"])

MAX_PAGE_SIZE = 100
MAX_RESULT_PAGE = 500      # a shortlist page; the full ranking stays in match_results
_STATUSES = sorted(status.value for status in RunStatus)


def run_out(run: WorkflowRun) -> RunOut:
    """The projection row as an API response: public id, no engine or table identifiers."""
    return RunOut(id=str(run.external_id), domain=run.domain, journey=run.journey, status=run.status,
                  input_ref=dict(run.input_ref or {}), result_summary=dict(run.result_summary or {}),
                  error_code=run.error_code, error_summary=run.error_summary, created_at=run.created_at,
                  started_at=run.started_at, completed_at=run.completed_at)


async def _run(session: AsyncSession, scope: TenantScope, run_id: str) -> WorkflowRun:
    """One run of this project, or `WFL-NFD-0002`. A run of another tenant is not found, never forbidden."""
    run = await WorkflowRunRepository(session).get_by_external_id(scope.client_id, scope.project_id, run_id)
    if run is None:
        raise AppError(OrchErrors.RUN_NOT_FOUND)
    return run


@router.get("", response_model=list[RunOut], response_model_by_alias=True)
async def list_runs(journey: Optional[str] = Query(default=None, max_length=64),
                    status: Optional[str] = Query(default=None, max_length=24),
                    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
                    offset: int = Query(default=0, ge=0),
                    user: UserClaims = Depends(current_user),
                    scope: TenantScope = Depends(tenant_scope),
                    session: AsyncSession = Depends(get_session)):
    """A page of this project's runs, newest first. Filters: `journey`, `status`.

    An unknown status is rejected rather than silently returning nothing, so a typo in a dashboard filter is
    visible instead of looking like "no runs".
    """
    if status is not None and status not in _STATUSES:
        raise AppError(Errors.VALIDATION_FAILED, fields=[{
            "field": "status", "message": f"expected one of: {', '.join(_STATUSES)}"}])
    runs = await WorkflowRunRepository(session).list_for_project(
        scope.client_id, scope.project_id, journey=journey, status=status, limit=limit, offset=offset)
    return [run_out(run) for run in runs]


@router.post("/bulk-match", response_model=BulkMatchStartedOut, response_model_by_alias=True,
             status_code=202)
async def start_bulk_match_run(body: BulkMatchRequest, user: UserClaims = Depends(current_user),
                               scope: TenantScope = Depends(tenant_scope)):
    """Start a bulk match of this project's candidates against one job description (J3).

    Returns **202**: the run is accepted, not finished. Poll `GET /runs/{id}` for its status and
    `GET /runs/{id}/results` for the shortlist as it fills.

    The business key is the job description, so submitting the same one twice reattaches to the single run
    (`reattached: true`) instead of paying for a thousand screenings again.

    There is deliberately **no fallback** when no workflow engine is configured: grading a project's whole
    candidate pool inside a request is not a degraded version of this journey, so the caller is told the
    capability is unavailable rather than being quietly given something worse.
    """
    launched = await start_bulk_match(scope, body.jd_document_id, top_n=body.top_n,
                                      max_subjects=body.max_subjects)
    if launched is None:
        raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=30)
    return BulkMatchStartedOut(id=launched.run_external_id, status=launched.status,
                               reattached=launched.reattached)


@router.get("/{run_id}/results", response_model=list[MatchResultOut], response_model_by_alias=True)
async def list_run_results(run_id: str, limit: int = Query(default=50, ge=1, le=MAX_RESULT_PAGE),
                           offset: int = Query(default=0, ge=0),
                           include_failed: bool = Query(default=False),
                           user: UserClaims = Depends(current_user),
                           scope: TenantScope = Depends(tenant_scope),
                           session: AsyncSession = Depends(get_session)):
    """A page of one run's per-subject results, best score first - the shortlist.

    Subjects that could not be graded are **excluded by default** but available with
    `includeFailed=true`: a shortlist should not be padded with failures, and a run's honesty about how many
    it could not grade is in the run's own summary.

    Ranks are assigned when the run finishes, so a page read mid-run is ordered by score but unnumbered.
    """
    run = await _run(session, scope, run_id)
    results = MatchResultRepository(session)
    rows = await results.ranked(scope.client_id, int(run.id), limit=limit, offset=offset)
    subjects = await SubjectRepository(session).by_ids(
        scope.client_id, scope.project_id, [int(row.subject_id) for row in rows])
    out = []
    for row in rows:
        if row.error_code and not include_failed:
            continue
        subject = subjects.get(int(row.subject_id))
        out.append(MatchResultOut(
            subject_id=int(row.subject_id), subject_ref=subject.graph_ref if subject else "",
            display_name=(subject.display_name if subject else ""), rank=row.rank,
            score=float(row.score) if row.score is not None else None, band=row.band,
            recommendation=row.recommendation, grades=dict(row.grades or {}),
            breakdown=dict(row.breakdown or {}), error_code=row.error_code))
    return out


@router.get("/{run_id}", response_model=RunOut, response_model_by_alias=True)
async def get_run(run_id: str, user: UserClaims = Depends(current_user),
                  scope: TenantScope = Depends(tenant_scope),
                  session: AsyncSession = Depends(get_session)):
    """One run: its status, what it was about and how it ended."""
    return run_out(await _run(session, scope, run_id))


@router.post("/{run_id}/cancel", response_model=RunCancelOut, response_model_by_alias=True)
async def cancel_run(run_id: str, user: UserClaims = Depends(current_user),
                     scope: TenantScope = Depends(tenant_scope),
                     session: AsyncSession = Depends(get_session)):
    """Stop a run that is still going.

    Cancellation is cooperative and propagates: the engine cancels the workflow, the workflow cancels its
    activity, the activity cancels its MCP call and the LLM stream closes, so the spend stops at once
    (CLAUDE.md section 6). The projection is written by the launcher, because a cancelled workflow may not
    start further activities and so cannot report its own cancellation.
    """
    run = await _run(session, scope, run_id)
    if run.status in RUN_TERMINAL_STATUSES:
        raise AppError(OrchErrors.RUN_ALREADY_FINISHED)
    launcher = get_container().launcher
    if not launcher.enabled:
        # The row says "running" but there is no engine to stop: say so instead of reporting a cancellation
        # that did not happen.
        raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=5)
    cancelled = await launcher.cancel(scope.client_id, run.temporal_workflow_id,
                                      reason=f"cancelled by user {user.user_id}")
    return RunCancelOut(id=run_id, cancelled=cancelled)
