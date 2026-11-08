"""Request dependencies. The access token (and its authz version) was verified by JWTAuthMiddleware; these only
read the result."""
from fastapi import Depends, Request

from agent_common.constants import Role
from agent_common.errors import AppError, Errors
from agent_common.security import UserClaims
from auth_service.container import Container, get_container


def current_user(request: Request) -> UserClaims:
    """Claims of the signed-in user. Raises AppError(AUTH-MIS-0001) when the middleware set none."""
    claims = getattr(request.state, "user", None)
    if claims is None:
        raise AppError(Errors.AUTH_TOKEN_MISSING)
    return claims


async def require_admin(claims: UserClaims = Depends(current_user),
                        c: Container = Depends(get_container)) -> UserClaims:
    """Admins only: the token says role=admin AND the user's current permissions agree (authz snapshot at the
    current version: Redis, database only on a miss). A demotion or deactivation bumps the version, so it applies
    at once. Raises AppError(AUTH-FBD-0001)."""
    if claims.role != Role.ADMIN:
        raise AppError(Errors.AUTH_FORBIDDEN)
    snapshot = await c.authz.snapshot(claims.user_id, claims.client_id)
    if snapshot is None or not snapshot.active or snapshot.role != Role.ADMIN or snapshot.client_id != claims.client_id:
        raise AppError(Errors.AUTH_FORBIDDEN)
    return claims
