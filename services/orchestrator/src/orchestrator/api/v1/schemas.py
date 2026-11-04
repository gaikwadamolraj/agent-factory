"""API v1 contracts (camelCase on the wire, never table/column names)."""
from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from ats_common.models import ApiModel, ApiResponse


# ---------------------------------------------------------------- auth
class LoginRequest(ApiModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=8, max_length=128)


class ProjectOut(ApiResponse):
    id: int
    name: str
    description: Optional[str] = None


class ClientOut(ApiResponse):
    id: int
    name: str


class UserOut(ApiResponse):
    id: int
    username: str
    role: str


class SessionOut(ApiResponse):
    """Who am I: user, client and the projects the user may work in. Tokens travel only in httpOnly cookies;
    expires_in tells the UI when to refresh silently (seconds)."""
    user: UserOut
    client: ClientOut
    projects: list[ProjectOut]
    expires_in: Optional[int] = None


# ---------------------------------------------------------------- admin: users of the admin's client
ASSIGNABLE_ROLES = r"^(admin|recruiter)$"


class AdminUserOut(ApiResponse):
    id: int
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime


class AdminUserCreate(ApiModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    email: Optional[str] = Field(default=None, max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="recruiter", pattern=ASSIGNABLE_ROLES)


class AdminUserUpdate(ApiModel):
    role: Optional[str] = Field(default=None, pattern=ASSIGNABLE_ROLES)
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)


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
