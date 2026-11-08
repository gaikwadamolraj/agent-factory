"""registry-service ASGI app: one FastAPI app serving the hardened REST wrapper and mounting the secured MCP front door.

Entry point: `uvicorn registry_service.server:app`. Both front doors share the pipeline in registry_service.dispatch.

Composition:
* FastAPI middleware (outermost first): request context -> security headers -> CORS -> body-size limit on /api.
* REST routes under /api/v1 (registry_service.api) + /healthz.
* The MCP server mounted at /mcp behind ServiceAuth (service token, aud registry-service, ALLOWED_CALLERS) + the
  per-tool TOOL_POLICY allow-list. The FastAPI middleware above also covers the mount, so it is not re-wrapped with
  request-context/security-headers.
* Lifespan starts the container (Redis + DB pool + shared services) and the MCP session manager.
"""
import atexit
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_common.constants import HEADER_CLIENT_ID, HEADER_CSRF, HEADER_PROJECT_ID, HEADER_REQUEST_ID
from agent_common.constants import JWT_AUDIENCE_REGISTRY
from agent_common.fastapi_errors import install_error_handlers
from agent_common.logging import get_logger, log_event
from agent_common.middleware import (BodySizeLimitMiddleware, PathRules, RequestContextMiddleware,
                                   SecurityHeadersMiddleware, ServiceAuthMiddleware)
from agent_common.middleware.tool_policy import ToolPolicyMiddleware
from agent_common.telemetry import instrument_asgi, setup_observability

from registry_service import api
from registry_service.config import FactorySettings, get_settings
from registry_service.constants import API_V1, MCP_PATH
from registry_service.container import Container, set_active_container, start_container, stop_container
from registry_service.errors import FactoryErrors
from registry_service.tools import build_mcp_server

log = get_logger("registry_service")
_ = FactoryErrors  # register service error codes at import


def _build_mounted_mcp(settings: FactorySettings, verifier):
    """The MCP streamable app (served at the mount root) wrapped with only the service-token + tool-policy layers;
    the FastAPI middleware provides request context and security headers for the whole app."""
    mcp = build_mcp_server(settings)
    from mcp.server.transport_security import TransportSecuritySettings

    inner = mcp.streamable_http_app(
        streamable_http_path="/",                 # mounted at /mcp, so serve at the mount root
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))  # ClusterIP only
    rules = PathRules(protected=("/",))
    secured = ToolPolicyMiddleware(inner, rules, settings.tool_policy_map())
    secured = ServiceAuthMiddleware(secured, verifier, JWT_AUDIENCE_REGISTRY, rules, settings.caller_set())
    return inner, secured


def create_app(settings: FactorySettings, *, container: Optional[Container] = None) -> FastAPI:
    """Build the FastAPI app. When `container` is given (tests), the lifespan uses it and does not start/stop it or the
    MCP session manager; otherwise the lifespan starts the real container and the MCP session manager."""
    # The MCP mount verifies service tokens; use the container's verifier in tests, else build one from settings (the
    # container does not exist until startup, and this verifier only verifies incoming /mcp tokens).
    from agent_common.security import build_jwks_verifier

    verifier = container.verifier if container is not None else build_jwks_verifier(settings)
    inner_mcp, secured_mcp = _build_mounted_mcp(settings, verifier)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        managed = container is None
        active = container if container is not None else await start_container(settings)
        set_active_container(active)
        log_event(log, "service.started", env=settings.app_env, tools=len(active.tool_policy) or None)
        if managed:
            async with inner_mcp.router.lifespan_context(app):     # MCP session manager
                yield
        else:
            yield
        set_active_container(None)
        if managed:
            await stop_container(active)

    docs = not settings.is_prod                   # no OpenAPI/docs in PROD
    app = FastAPI(title="ATS Agent Factory", version="1.0.0", lifespan=lifespan,
                  docs_url="/docs" if docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if docs else None)
    install_error_handlers(app)

    # Middleware (reverse order: add outermost last). Body limit only guards /api; the MCP layer has its own limit.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_kb * 1024,
                       rules=PathRules(protected=(API_V1,)))
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list,
                       allow_credentials="*" not in settings.cors_origin_list,
                       allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", HEADER_REQUEST_ID, HEADER_CSRF,
                                      HEADER_CLIENT_ID, HEADER_PROJECT_ID],
                       expose_headers=[HEADER_REQUEST_ID, "Retry-After"])
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, service_name=settings.service_name,
                       expose_served_by=not settings.is_prod)

    app.include_router(api.router)
    app.mount(MCP_PATH, secured_mcp)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    return app


# Module-level ASGI app for uvicorn (production entry point).
settings = get_settings()
observability = setup_observability(settings)
atexit.register(observability.shutdown)
_fastapi_app = create_app(settings)
app = instrument_asgi(_fastapi_app) if settings.otel_enabled else _fastapi_app


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
