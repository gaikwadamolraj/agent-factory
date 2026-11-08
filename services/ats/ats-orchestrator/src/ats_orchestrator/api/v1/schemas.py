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
