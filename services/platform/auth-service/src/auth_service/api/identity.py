"""/api/v1/identity: identity writes the auth-service owns, called by registry-service with a service token.

Ownership split (docs/impl_agent_factory.md section 6b): registry-service owns clients/projects/groups/policies; the
auth-service owns users, credentials and invites. registry-service delegates "create user / set role / deactivate /
invite" here. These endpoints are authenticated with a service token (aud `auth-service`) from an allowed caller
(ServiceAuthMiddleware is for pure-MCP apps, so this FastAPI service checks the token in a dependency); they are NOT
user-token paths (excluded from JWTAuthMiddleware by a public prefix in app.py). The caller passes the clientId
explicitly, since a service token carries no tenant.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.constants import JWT_AUDIENCE_AUTH, Role
from agent_common.db import UserInviteRepository, get_session
from agent_common.db.platform_tables import UserInvite
from agent_common.db.repositories import UserRepository
from agent_common.db.tables import User
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.models import ApiModel, ApiResponse
from agent_common.security import verify_service_token
from auth_service.container import Container, get_container
from auth_service.errors import AuthErrors
from pydantic import Field

router = APIRouter(prefix="/identity", tags=["identity"])
log = get_logger(__name__)

UNUSABLE_PASSWORD = "!"       # a sentinel hash no password can match (pending users, until they accept an invite)
_ROLE = r"^(admin|recruiter|viewer|client_admin|super_admin)$"


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def require_identity_caller(request: Request, c: Container = Depends(get_container)) -> str:
    """Verify a service token (aud auth-service) from an allowed caller. Raises 401/403 otherwise."""
    token = _bearer(request)
    if not token:
        raise AppError(Errors.AUTH_TOKEN_MISSING)
    caller = await verify_service_token(c.tokens.verifier, token, JWT_AUDIENCE_AUTH)
    if caller not in c.settings.identity_caller_set():
        log_event(log, "identity.caller_forbidden", caller=caller)
        raise AppError(Errors.SERVICE_FORBIDDEN)
    return caller


# --------------------------------------------------------------------------------- schemas
class IdentityUserCreate(ApiModel):
    client_id: int = Field(ge=1)
    username: str = Field(min_length=3, max_length=64)
    email: Optional[str] = Field(default=None, max_length=255)
    role: str = Field(default="recruiter", pattern=_ROLE)
    active: bool = True


class IdentityUserUpdate(ApiModel):
    client_id: int = Field(ge=1)
    role: Optional[str] = Field(default=None, pattern=_ROLE)
    active: Optional[bool] = None


class IdentityUserOut(ApiResponse):
    id: int
    username: str
    role: str
    is_active: bool


class IdentityInviteCreate(ApiModel):
    client_id: int = Field(ge=1)
    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    role: str = Field(default="recruiter", pattern=_ROLE)
    group_ids: list[int] = Field(default_factory=list, max_length=100)
    project_ids: list[int] = Field(default_factory=list, max_length=100)
    invited_by: int = Field(ge=1)


class RevokeInvite(ApiModel):
    client_id: int = Field(ge=1)


# --------------------------------------------------------------------------------- users
@router.post("/users", response_model=IdentityUserOut, response_model_by_alias=True, status_code=201)
async def create_user(body: IdentityUserCreate, caller: str = Depends(require_identity_caller),
                      session: AsyncSession = Depends(get_session), c: Container = Depends(get_container)):
    """Create a user in a client. The password is set later (invite accept / reset), so the stored hash is unusable."""
    users = UserRepository(session)
    if await users.get_by_username(body.username) is not None:
        raise AppError(AuthErrors.USER_EXISTS)
    user = User(client_id=body.client_id, username=body.username, email=body.email,
                password_hash=UNUSABLE_PASSWORD, role=body.role, is_active=body.active)
    await users.add(user)
    log_event(log, "identity.user_created", caller=caller, target_user_id=user.id, role=user.role)
    return IdentityUserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=IdentityUserOut, response_model_by_alias=True)
async def update_user(user_id: int, body: IdentityUserUpdate, caller: str = Depends(require_identity_caller),
                      session: AsyncSession = Depends(get_session), c: Container = Depends(get_container)):
    """Change a user's role and/or active state (never across clients). Bumps the user's authz version."""
    users = UserRepository(session)
    user = await users.get_in_client(user_id, body.client_id)
    if user is None:
        raise AppError(AuthErrors.USER_NOT_FOUND)
    loses_admin = user.role == Role.ADMIN and user.is_active and (
        (body.role is not None and body.role != Role.ADMIN) or body.active is False)
    if loses_admin and await users.count_active_admins(body.client_id, Role.ADMIN) <= 1:
        raise AppError(AuthErrors.LAST_ADMIN)
    if body.role is not None:
        user.role = body.role
    if body.active is not None:
        user.is_active = body.active
    session.add(user)
    await session.commit()                     # commit BEFORE the bump so no reader caches the old state
    await c.authz.bump_user(user.id)
    log_event(log, "identity.user_updated", caller=caller, target_user_id=user.id, role=user.role,
              active=user.is_active)
    return IdentityUserOut.model_validate(user)


# --------------------------------------------------------------------------------- invites
@router.post("/invites", status_code=201)
async def create_invite(body: IdentityInviteCreate, caller: str = Depends(require_identity_caller),
                        c: Container = Depends(get_container)):
    """Create a pending user + a signed, single-use invite token; return the shareable link (delivered by the
    notifier; logged/returned today). The token's hash is stored, never the token."""
    token, token_hash, expires_at = c.invites.create(body.client_id)
    async with c.db.tenant_session(body.client_id) as session:     # user_invites + users under tenant context (RLS)
        users = UserRepository(session)
        if await users.get_by_username(body.email) is None:
            session.add(User(client_id=body.client_id, username=body.email, email=body.email,
                             password_hash=UNUSABLE_PASSWORD, role=body.role, is_active=False))
            await session.flush()
        invite = UserInvite(client_id=body.client_id, email=body.email, role=body.role, groups=body.group_ids,
                            projects=body.project_ids, token_hash=token_hash, status="pending",
                            expires_at=expires_at, invited_by=body.invited_by)
        session.add(invite)
        await session.flush()
        external_id = invite.external_id
    log_event(log, "identity.invite_created", caller=caller, client_id=body.client_id)
    return {"id": str(external_id), "email": body.email, "role": body.role,
            "inviteLink": c.invites.build_link(token), "expiresAt": expires_at.isoformat()}


@router.get("/invites")
async def list_invites(clientId: int, caller: str = Depends(require_identity_caller),  # noqa: N803
                       c: Container = Depends(get_container)):
    """Pending invites of a client."""
    async with c.db.tenant_session(clientId) as session:
        rows = await UserInviteRepository(session).list_pending(clientId)
        items = [{"id": str(r.external_id), "email": r.email, "role": r.role, "status": r.status,
                  "expiresAt": r.expires_at.isoformat()} for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(invite_id: str, body: RevokeInvite, caller: str = Depends(require_identity_caller),
                        c: Container = Depends(get_container)):
    """Revoke a pending invite (it can no longer be accepted)."""
    async with c.db.tenant_session(body.client_id) as session:
        from sqlmodel import select

        invite = (await session.exec(select(UserInvite).where(
            UserInvite.external_id == invite_id, UserInvite.client_id == body.client_id))).first()
        if invite is None:
            raise AppError(AuthErrors.INVITE_NOT_FOUND)
        invite.status = "revoked"
        session.add(invite)
    log_event(log, "identity.invite_revoked", caller=caller, client_id=body.client_id)
    return {"id": invite_id, "status": "revoked"}
