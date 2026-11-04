"""/api/v1/admin: user management for admins, always limited to the admin's own client.

Normal users get 403 (AUTH-FBD-0001). Role and active-state changes apply to the user's next token refresh
(at most the access-token lifetime); a disabled user cannot refresh or sign in.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from ats_common.constants import Role
from ats_common.db import get_session
from ats_common.db.repositories import MembershipRepository, ProjectRepository, UserRepository
from ats_common.db.tables import User
from ats_common.errors import AppError
from ats_common.logging import get_logger, log_event
from ats_common.security import UserClaims, hash_password
from orchestrator.api.deps import require_admin
from orchestrator.api.v1.schemas import AdminUserCreate, AdminUserOut, AdminUserUpdate
from orchestrator.errors import OrchErrors

router = APIRouter(prefix="/admin", tags=["admin"])
log = get_logger(__name__)


@router.get("/users", response_model=list[AdminUserOut], response_model_by_alias=True)
async def list_users(admin: UserClaims = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """All users of the admin's client."""
    return [AdminUserOut.model_validate(u) for u in await UserRepository(session).list_for_client(admin.client_id)]


@router.post("/users", response_model=AdminUserOut, response_model_by_alias=True, status_code=201)
async def create_user(body: AdminUserCreate, admin: UserClaims = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    """Create a user in the admin's client and give access to all of the client's active projects."""
    users = UserRepository(session)
    if await users.get_by_username(body.username):
        raise AppError(OrchErrors.USER_EXISTS)
    user = User(client_id=admin.client_id, username=body.username, email=body.email,
                password_hash=hash_password(body.password), role=body.role)
    try:
        await users.add(user)
    except IntegrityError:                       # created concurrently with the same username
        raise AppError(OrchErrors.USER_EXISTS) from None
    projects = await ProjectRepository(session).list_active_for_client(admin.client_id)
    await MembershipRepository(session).add(user.id, [p.id for p in projects])
    log_event(log, "admin.user_created", target_user_id=user.id, target_username=user.username, role=user.role)
    return AdminUserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=AdminUserOut, response_model_by_alias=True)
async def update_user(user_id: int, body: AdminUserUpdate, admin: UserClaims = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    """Change role, enable/disable, or set a new password."""
    users = UserRepository(session)
    user = await users.get_in_client(user_id, admin.client_id)
    if user is None:
        raise AppError(OrchErrors.USER_NOT_FOUND)

    loses_admin = user.role == Role.ADMIN and user.is_active and (
        (body.role is not None and body.role != Role.ADMIN) or body.is_active is False)
    if loses_admin and await users.count_active_admins(admin.client_id, Role.ADMIN) <= 1:
        raise AppError(OrchErrors.LAST_ADMIN)

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
        await session.flush()
    log_event(log, "admin.user_updated", target_user_id=user.id, target_username=user.username, changed=changed)
    return AdminUserOut.model_validate(user)
