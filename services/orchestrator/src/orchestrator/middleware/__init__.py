"""Middleware stack of the orchestrator (common middlewares from ats_common + service-specific ones).

Order (outermost first): OTEL span -> request context -> security headers -> CORS -> body size limit ->
JWT auth (cookie or bearer, CSRF guard) -> activity -> tenant headers -> app.
`install_middleware` registers them in reverse because add_middleware wraps outermost last.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ats_common.constants import HEADER_CLIENT_ID, HEADER_CSRF, HEADER_PROJECT_ID, HEADER_REQUEST_ID
from ats_common.db.repositories import MembershipRepository
from ats_common.middleware import (BodySizeLimitMiddleware, JWTAuthMiddleware, PathRules, RequestContextMiddleware,
                                   SecurityHeadersMiddleware, TenantMiddleware)
from ats_common.redis_keys import Keys, build_key, ttl
from orchestrator.config import OrchestratorSettings
from orchestrator.constants import AUTH_API_PREFIXES, PUBLIC_API_PATHS, TENANT_API_PREFIXES
from orchestrator.container import get_container


async def is_project_member(user_id: int, client_id: int, project_id: int) -> bool:
    """Membership check with a short Redis cache (positive and negative answers)."""
    c = get_container()
    key = build_key(Keys.TENANT_MEMBERSHIP, c.settings.app_env, client_id=client_id, project_id=project_id,
                    user_id=user_id)
    cached = await c.redis.get(key)
    if cached is not None:
        return cached == "1"
    async with c.db.session() as session:
        member = await MembershipRepository(session).is_member(user_id, client_id, project_id)
    await c.redis.setex(key, ttl(Keys.TENANT_MEMBERSHIP), "1" if member else "0")
    return member


class ActivityMiddleware:
    """Records authenticated users as active (feeds the ats_active_users gauge)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        claims = scope.get("state", {}).get("user") if scope["type"] == "http" else None
        if claims is not None:
            get_container().active_users.touch(claims.user_id, claims.client_id)
        await self.app(scope, receive, send)


def install_middleware(app: FastAPI, settings: OrchestratorSettings) -> None:
    app.add_middleware(TenantMiddleware, rules=PathRules(protected=TENANT_API_PREFIXES), is_member=is_project_member)
    app.add_middleware(ActivityMiddleware)
    app.add_middleware(JWTAuthMiddleware, secret=settings.require_jwt_secret(), algorithm=settings.jwt_algorithm,
                       rules=PathRules(protected=AUTH_API_PREFIXES, public=PUBLIC_API_PATHS))
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list,
                       allow_credentials="*" not in settings.cors_origin_list,
                       allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", HEADER_CLIENT_ID, HEADER_PROJECT_ID,
                                      HEADER_REQUEST_ID, HEADER_CSRF],
                       expose_headers=[HEADER_REQUEST_ID, "Retry-After"])
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, service_name=settings.service_name,
                       expose_served_by=not settings.is_prod)
