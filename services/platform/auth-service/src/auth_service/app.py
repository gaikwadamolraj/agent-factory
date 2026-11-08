"""FastAPI application of the auth-service (`create_app`); `auth_service.main:app` is the uvicorn entry point.

Middleware order (outermost first): request context -> security headers -> CORS -> body size limit -> access
token on /api/v1 (minus login/refresh/logout) -> routes. /oauth/token and /.well-known/* are outside /api/v1.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_common.constants import HEADER_CSRF, HEADER_REQUEST_ID
from agent_common.fastapi_errors import install_error_handlers
from agent_common.logging import get_logger, log_event
from agent_common.middleware import (BodySizeLimitMiddleware, JWTAuthMiddleware, PathRules, RequestContextMiddleware,
                                   SecurityHeadersMiddleware)
from auth_service.api import routers
from auth_service.config import AuthSettings
from auth_service.constants import API_V1, IDENTITY_PREFIX, PUBLIC_API_PATHS
from auth_service.container import start_container, stop_container
from auth_service.errors import AuthErrors
from auth_service.signing import KeyRing
from auth_service.tokens import TokenIssuer

log = get_logger("auth_service")
_ = AuthErrors  # register service error codes at import


def install_middleware(app: FastAPI, settings: AuthSettings, tokens: TokenIssuer) -> None:
    """Register the middlewares in reverse order (add_middleware wraps outermost last)."""
    app.add_middleware(JWTAuthMiddleware, verifier=tokens.verifier,
                       rules=PathRules(protected=(API_V1,), public=PUBLIC_API_PATHS,
                                       public_prefixes=(IDENTITY_PREFIX,)),   # identity uses a service token instead
                       authz=lambda: app.state.container.authz)     # stale authz version -> AUTH-EXP-0002
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_kb * 1024)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list,
                       allow_credentials="*" not in settings.cors_origin_list,
                       allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", HEADER_REQUEST_ID, HEADER_CSRF],
                       expose_headers=[HEADER_REQUEST_ID, "Retry-After"])
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, service_name=settings.service_name,
                       expose_served_by=not settings.is_prod)


def create_app(settings: AuthSettings, tokens: TokenIssuer | None = None) -> FastAPI:
    """Build the app. `tokens` defaults to the keys from the settings (AUTH_SIGNING_KEY[_PREVIOUS]).

    The lifespan connects Redis + database (`app.state.container`); tests set the container themselves.
    """
    tokens = tokens or TokenIssuer.from_settings(settings, KeyRing.from_settings(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = await start_container(settings, tokens)
        log_event(log, "service.started", env=settings.app_env, kid=tokens.keys.current.kid,
                  previous_kid=tokens.keys.previous.kid if tokens.keys.previous else None,
                  clients=app.state.container.clients.client_ids)
        yield
        await stop_container(app.state.container)

    docs = not settings.is_prod  # no OpenAPI/docs in PROD
    app = FastAPI(title="ATS Auth API", version="1.0.0", lifespan=lifespan,
                  docs_url="/docs" if docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if docs else None)
    install_error_handlers(app)
    install_middleware(app, settings, tokens)
    for router in routers:
        app.include_router(router)

    @app.get("/healthz", tags=["health"], include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    return app
