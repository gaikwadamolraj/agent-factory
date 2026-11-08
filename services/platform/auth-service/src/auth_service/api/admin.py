"""/api/v1/admin: user management for admins, always limited to the admin's own client.

Lives in the auth-service because users, passwords and roles are identity data: password hashing and the
"disabled users cannot sign in or refresh" rule stay in the one service that issues tokens.
Normal users get 403 (AUTH-FBD-0001). Every change of a user bumps their authz version (agent_common.authz), so it
applies at once in every service: older access tokens get AUTH-EXP-0002 (the UI refreshes silently) and cached
permissions are re-read. A disabled user cannot refresh or sign in.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.constants import Role
from agent_common.db import get_session
from agent_common.db.repositories import MembershipRepository, ProjectRepository, UserRepository
from agent_common.db.tables import User
from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from agent_common.security import UserClaims
from auth_service.api.deps import require_admin
from auth_service.api.schemas import AdminUserCreate, AdminUserOut, AdminUserUpdate
from auth_service.container import Container, get_container
from auth_service.errors import AuthErrors
from auth_service.passwords import hash_password

router = APIRouter(prefix="/admin", tags=["admin"])
log = get_logger(__name__)


@router.get("/users", response_model=list[AdminUserOut], response_model_by_alias=True)
async def list_users(admin: UserClaims = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """All users of the admin's client."""
    return [AdminUserOut.model_validate(u) for u in await UserRepository(session).list_for_client(admin.client_id)]


@router.post("/users", response_model=AdminUserOut, response_model_by_alias=True, status_code=201)
async def create_user(body: AdminUserCreate, admin: UserClaims = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    """Create a user in the admin's client and give access to all of the client's active projects.
    Raises USR-CNF-0001 when the username is taken."""
    users = UserRepository(session)
    if await users.get_by_username(body.username):
        raise AppError(AuthErrors.USER_EXISTS)
    user = User(client_id=admin.client_id, username=body.username, email=body.email,
                password_hash=hash_password(body.password), role=body.role)
    try:
        await users.add(user)
    except IntegrityError:                       # created concurrently with the same username
        raise AppError(AuthErrors.USER_EXISTS) from None
    projects = await ProjectRepository(session).list_active_for_client(admin.client_id)
    await MembershipRepository(session).add(user.id, [p.id for p in projects])
    log_event(log, "admin.user_created", target_user_id=user.id, target_username=user.username, role=user.role)
    return AdminUserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=AdminUserOut, response_model_by_alias=True)
async def update_user(user_id: int, body: AdminUserUpdate, admin: UserClaims = Depends(require_admin),
                      session: AsyncSession = Depends(get_session), c: Container = Depends(get_container)):
    """Change role, enable/disable, or set a new password. Raises USR-NFD-0001 (not in the admin's client) and
    USR-INV-0001 (would leave the client without an active admin)."""
    users = UserRepository(session)
    user = await users.get_in_client(user_id, admin.client_id)
    if user is None:
        raise AppError(AuthErrors.USER_NOT_FOUND)

    loses_admin = user.role == Role.ADMIN and user.is_active and (
        (body.role is not None and body.role != Role.ADMIN) or body.is_active is False)
    if loses_admin and await users.count_active_admins(admin.client_id, Role.ADMIN) <= 1:
        raise AppError(AuthErrors.LAST_ADMIN)

    changed = []
    if body.role is not None and body.role != user.role:
        user.role = body.role
        changed.append("role")
    if body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        changed.append("is_active")
    if body.password:
        user.password_hash = hash_password(body.password)
        changed.append("password")
    if changed:
        session.add(user)
        await session.commit()              # commit BEFORE the bump: a reader must never cache the old state
        await c.authz.bump_user(user.id)    # under the new version
    log_event(log, "admin.user_updated", target_user_id=user.id, target_username=user.username, changed=changed)
    return AdminUserOut.model_validate(user)
