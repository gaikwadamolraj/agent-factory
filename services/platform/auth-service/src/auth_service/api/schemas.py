"""API contracts of the auth-service (camelCase on the wire, never table/column names).

Moved unchanged from the orchestrator, so the frontend sees the same JSON. Exception: the OAuth2 token response
uses the snake_case names RFC 6749 (section 5.1) prescribes.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from agent_common.models import ApiModel, ApiResponse

USERNAME_PATTERN = r"^[A-Za-z0-9_.@-]+$"
ASSIGNABLE_ROLES = r"^(admin|recruiter)$"


# ---------------------------------------------------------------- sign-in
class LoginRequest(ApiModel):
    username: str = Field(min_length=3, max_length=64, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=128)


class InviteAcceptRequest(ApiModel):
    """The invitee accepts their invite and sets their own password (hashed with the current pepper)."""
    token: str = Field(min_length=8, max_length=512)
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
class AdminUserOut(ApiResponse):
    id: int
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime


class AdminUserCreate(ApiModel):
    username: str = Field(min_length=3, max_length=64, pattern=USERNAME_PATTERN)
    email: Optional[str] = Field(default=None, max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="recruiter", pattern=ASSIGNABLE_ROLES)


class AdminUserUpdate(ApiModel):
    role: Optional[str] = Field(default=None, pattern=ASSIGNABLE_ROLES)
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)


# ---------------------------------------------------------------- OAuth2 / discovery (standard names)
class TokenOut(BaseModel):
    """RFC 6749 section 5.1 access token response."""
    access_token: str
    token_type: str
    expires_in: int
