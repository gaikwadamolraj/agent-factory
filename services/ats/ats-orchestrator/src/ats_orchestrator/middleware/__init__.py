"""Middleware stack of the orchestrator (common middlewares from agent_common + service-specific ones).

Order (outermost first): OTEL span -> request context -> security headers -> CORS -> body size limit ->
access token (cookie or bearer, CSRF guard; ES256, verified locally with the auth-service JWKS) -> activity ->
tenant headers (client = token cid, project membership from the versioned authz snapshot) -> app.
The access-token step also rejects tokens older than the user's authz version (AUTH-EXP-0002, agent_common.authz).
`install_middleware` registers them in reverse because add_middleware wraps outermost last.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_common.authz import AuthzService
from agent_common.constants import HEADER_CLIENT_ID, HEADER_CSRF, HEADER_PROJECT_ID, HEADER_REQUEST_ID
from agent_common.middleware import (BodySizeLimitMiddleware, JWTAuthMiddleware, PathRules, RequestContextMiddleware,
                                   SecurityHeadersMiddleware, TenantMiddleware)
from agent_common.security import build_jwks_verifier
from ats_orchestrator.config import OrchestratorSettings
from ats_orchestrator.constants import AUTH_API_PREFIXES, TENANT_API_PREFIXES
from ats_orchestrator.container import get_container


def current_authz() -> AuthzService:
    """The AuthzService of the running process (created at startup, after the middleware stack is built)."""
    return get_container().authz


class ActivityMiddleware:
    """Records authenticated users as active (feeds the agent_active_users gauge)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        claims = scope.get("state", {}).get("user") if scope["type"] == "http" else None
        if claims is not None:
            get_container().active_users.touch(claims.user_id, claims.client_id)
        await self.app(scope, receive, send)


def install_middleware(app: FastAPI, settings: OrchestratorSettings) -> None:
    app.add_middleware(TenantMiddleware, rules=PathRules(protected=TENANT_API_PREFIXES), authz=current_authz)
    app.add_middleware(ActivityMiddleware)
    app.add_middleware(JWTAuthMiddleware, verifier=build_jwks_verifier(settings),
                       rules=PathRules(protected=AUTH_API_PREFIXES), authz=current_authz)
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
