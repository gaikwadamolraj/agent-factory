"""Request dependencies. Identity and tenant were verified by the middleware; these only read them."""
from fastapi import Request

from agent_common.db.repositories import TenantScope
from agent_common.errors import AppError, Errors
from agent_common.security import UserClaims


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

