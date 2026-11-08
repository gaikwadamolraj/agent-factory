"""Request models for every registry-service tool (ApiModel: camelCase on the wire, snake_case in Python, extra forbid).

Ids exposed to the wire are the tables' `external_id` UUIDs (never internal BIGSERIAL ids). User/project/client ids
that reference the existing tenancy tables stay integers, matching those tables' integer primary keys.
"""
from typing import Optional
from uuid import UUID

from pydantic import Field

from agent_common.models import ApiModel

from registry_service.constants import PRINCIPAL_TYPES

_NAME = r"^[A-Za-z0-9 ._/-]+$"
_SLUG = r"^[a-z0-9-]+$"
_DOMAIN = r"^[a-z0-9_-]+$"
_ROLE = r"^(admin|recruiter|viewer|client_admin|super_admin)$"


class Empty(ApiModel):
    """A tool that takes no arguments."""


# --------------------------------------------------------------------------------- skills & versions
class CreateSkillRequest(ApiModel):
    domain: str = Field(min_length=1, max_length=64, pattern=_DOMAIN)
    name: str = Field(min_length=1, max_length=128, pattern=_DOMAIN)
    description: Optional[str] = Field(default=None, max_length=1000)


class SkillIdRequest(ApiModel):
    skill_id: UUID


class ResolveSkillRequest(ApiModel):
    """Runtime lookup by natural key: which version should an agent run right now, and what is in it.

    `version_number` pins an exact version (reproducing an old run); omitted means the skill's active version.
    A service caller also sends `client_id`, which dispatch.py turns into the tenant session.
    """
    domain: str = Field(min_length=1, max_length=64, pattern=_DOMAIN)
    name: str = Field(min_length=1, max_length=128, pattern=_DOMAIN)
    version_number: Optional[int] = Field(default=None, ge=1, le=100_000)
    client_id: Optional[int] = Field(default=None, ge=1, lt=2**31)


class VersionFile(ApiModel):
    path: str = Field(min_length=1, max_length=255)
    content: str = Field(default="", max_length=1_000_000)


class CreateDraftVersionRequest(ApiModel):
    skill_id: UUID
    workflow: dict = Field(default_factory=dict)
    files: list[VersionFile] = Field(default_factory=list, max_length=200)
    base_version_id: Optional[UUID] = None       # branch from this published/draft version's files when given


class VersionIdRequest(ApiModel):
    version_id: UUID


class UpdateDraftVersionRequest(ApiModel):
    version_id: UUID
    workflow: Optional[dict] = None
    files: Optional[list[VersionFile]] = Field(default=None, max_length=200)


class ApproveVersionRequest(ApiModel):
    version_id: UUID
    comment: Optional[str] = Field(default=None, max_length=2000)


class RejectVersionRequest(ApiModel):
    version_id: UUID
    reason: str = Field(min_length=1, max_length=1000)


class SetTestersRequest(ApiModel):
    version_id: UUID
    tester_user_ids: list[int] = Field(default_factory=list, max_length=200)


# --------------------------------------------------------------------------------- policies & groups
class ListPoliciesRequest(ApiModel):
    principal_type: Optional[str] = None
    principal_id: Optional[int] = None


class CreatePolicyRequest(ApiModel):
    principal_type: str = Field(pattern=r"^(user|group|project|client)$")
    principal_id: int = Field(ge=1)
    name: Optional[str] = Field(default=None, max_length=128, pattern=_NAME)
    document: dict = Field(default_factory=dict)
    enabled: bool = True


class UpdatePolicyRequest(ApiModel):
    policy_id: UUID
    name: Optional[str] = Field(default=None, max_length=128, pattern=_NAME)
    document: Optional[dict] = None
    enabled: Optional[bool] = None


class PolicyIdRequest(ApiModel):
    policy_id: UUID


class SimulateRequest(ApiModel):
    user_id: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=256)
    resource_client_id: Optional[int] = None
    resource_project_id: Optional[int] = None
    resource_author_id: Optional[int] = None


class CreateGroupRequest(ApiModel):
    name: str = Field(min_length=1, max_length=128, pattern=_NAME)
    description: Optional[str] = Field(default=None, max_length=1000)


class GroupIdRequest(ApiModel):
    group_id: UUID


class UpdateGroupRequest(ApiModel):
    group_id: UUID
    name: Optional[str] = Field(default=None, max_length=128, pattern=_NAME)
    description: Optional[str] = Field(default=None, max_length=1000)


class GroupMemberRequest(ApiModel):
    group_id: UUID
    user_id: int = Field(ge=1)


# --------------------------------------------------------------------------------- notifications (self-scoped)
class ListNotificationsRequest(ApiModel):
    unread_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class NotificationIdRequest(ApiModel):
    notification_id: UUID


# --------------------------------------------------------------------------------- projects
class CreateProjectRequest(ApiModel):
    slug: str = Field(min_length=1, max_length=64, pattern=_SLUG)
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)


class ProjectIdRequest(ApiModel):
    project_id: int = Field(ge=1)


class UpdateProjectRequest(ApiModel):
    project_id: int = Field(ge=1)
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    is_active: Optional[bool] = None


class ProjectMemberRequest(ApiModel):
    project_id: int = Field(ge=1)
    user_id: int = Field(ge=1)


# --------------------------------------------------------------------------------- clients & super admin
class CreateClientRequest(ApiModel):
    slug: str = Field(min_length=1, max_length=64, pattern=_SLUG)
    name: str = Field(min_length=1, max_length=200)


class AssumeClientRequest(ApiModel):
    client_id: int = Field(ge=1)


class GrantSuperAdminRequest(ApiModel):
    user_id: int = Field(ge=1)


# --------------------------------------------------------------------------------- delegated identity
class InviteUserRequest(ApiModel):
    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    role: str = Field(default="recruiter", pattern=_ROLE)
    group_ids: list[int] = Field(default_factory=list, max_length=100)
    project_ids: list[int] = Field(default_factory=list, max_length=100)


class CreateUserRequest(ApiModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    email: Optional[str] = Field(default=None, max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    role: str = Field(default="recruiter", pattern=_ROLE)


class SetUserRoleRequest(ApiModel):
    user_id: int = Field(ge=1)
    role: str = Field(pattern=_ROLE)


class UserIdRequest(ApiModel):
    user_id: int = Field(ge=1)


class InviteIdRequest(ApiModel):
    invite_id: UUID


def is_valid_principal_type(value: str) -> bool:
    """Guard for the optional principal_type filter."""
    return value in PRINCIPAL_TYPES
