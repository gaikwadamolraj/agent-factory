"""Request dependencies. Identity and tenant were verified by the middleware; these only read them."""
from fastapi import Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from ats_common.constants import Role
from ats_common.db import get_session
from ats_common.db.repositories import TenantScope, UserRepository
from ats_common.errors import AppError, Errors
from ats_common.security import UserClaims


def current_user(request: Request) -> UserClaims:
    claims = getattr(request.state, "user", None)
    if claims is None:
        raise AppError(Errors.AUTH_TOKEN_MISSING)
    return claims


def tenant_scope(request: Request) -> TenantScope:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise AppError(Errors.TENANT_HEADERS_MISSING)
    return TenantScope(client_id=tenant[0], project_id=tenant[1])


async def require_admin(claims: UserClaims = Depends(current_user),
                        session: AsyncSession = Depends(get_session)) -> UserClaims:
    """Admins only. Checks the database, not just the token, so a demoted or disabled admin loses access at once."""
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active or user.role != Role.ADMIN:
        raise AppError(Errors.AUTH_FORBIDDEN)
    return claims
