"""API v1 contracts (camelCase on the wire, never table/column names)."""
from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from agent_common.models import ApiModel, ApiResponse


# ---------------------------------------------------------------- chats
class ChatCreate(ApiModel):
    title: Optional[str] = Field(default=None, max_length=200)


class ChatUpdate(ApiModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    job_description: Optional[str] = Field(default=None, max_length=50_000)


class DocumentOut(ApiResponse):
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    pages: int
    created_at: datetime


class ChatOut(ApiResponse):
    id: str
    title: str
    has_job_description: bool
    has_resume: bool
    last_score: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class MessageOut(ApiResponse):
    id: int
    role: str
    content: str
    data: dict[str, Any]
    created_at: datetime


class ChatDetail(ChatOut):
    job_description: Optional[str] = None
    resume_document: Optional[DocumentOut] = None
    jd_document: Optional[DocumentOut] = None
    running_turn_id: Optional[str] = None
    messages: list[MessageOut]


class DocumentTextOut(ApiResponse):
    kind: str
    filename: str
    text: str


class AbortOut(ApiResponse):
    turn_id: str
    aborted: bool


# ------------------------------------------------------- durable runs
class RunOut(ApiResponse):
    """One durable run, as the product sees it (`workflow_runs`, docs/impl_temporal.md section 3).

    Deliberately the projection and nothing else: no Temporal workflow or run id, no internal row ids, no
    error text from an exception. `input_ref` and `result_summary` hold references and small values only,
    which is enforced where they are written, so they are safe to return as they are.
    """

    id: str                                    # the run's public id (external_id)
    domain: str
    journey: str
    status: str
    input_ref: dict[str, Any] = {}
    result_summary: dict[str, Any] = {}
    error_code: Optional[str] = None
    error_summary: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RunCancelOut(ApiResponse):
    id: str
    cancelled: bool


class BulkMatchRequest(ApiModel):
    """Start a bulk match: one job description against this project's candidates (J3).

    The caller names the job description and the bounds it wants; everything else - the pack, the task
    queue, the grading concurrency - is server-side policy, because a client must not be able to raise the
    platform's ceiling on LLM work.
    """

    jd_document_id: int = Field(gt=0)
    top_n: Optional[int] = Field(default=None, gt=0, le=500)
    max_subjects: Optional[int] = Field(default=None, gt=0, le=5000)


class BulkMatchStartedOut(ApiResponse):
    """What the caller needs to follow the run it just started (or reattached to)."""

    id: str                    # the run's public id; poll GET /runs/{id}
    status: str
    reattached: bool           # True when an equivalent run already existed and nothing new was started


class MatchResultOut(ApiResponse):
    """One subject's place in a match run - a row of the shortlist.

    References and small values only: the grades and the rubric's component points explain the score
    without re-running anything, and nothing here carries document content.
    """

    subject_id: int
    subject_ref: str
    display_name: str
    rank: Optional[int] = None
    score: Optional[float] = None
    band: str = ""
    recommendation: str = ""
    grades: dict[str, Any] = {}
    breakdown: dict[str, Any] = {}
    error_code: Optional[str] = None
