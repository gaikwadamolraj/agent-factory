"""The hardened REST wrapper - the main attack surface (docs/impl_registry_service.md section 3).

`POST /api/v1/factory/{tool}` turns every catalog tool into an endpoint with no per-tool handler code; `POST
/api/v1/mcp/{server}/{tool}` proxies a tool on another registered MCP server. Both authenticate (user JWT aud
ats-api, with the authz staleness check, OR a service token aud registry-service), apply a per-principal rate-limit
hook, then hand off to the shared pipeline (dispatch) which authorizes (default deny), validates, sets tenant
context and runs the operation. Identity comes only from the verified token - never from X-Auth-* headers.
"""
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_common.constants import HEADER_CLIENT_ID, JWT_AUDIENCE_REGISTRY
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.redis_keys import Keys, build_key, digest, ttl
from agent_common.security import verify_service_token, verify_user_token

from registry_service.constants import CATALOGUE_PREFIX, FACTORY_PREFIX, NOTIFICATIONS_PREFIX
from registry_service.container import Container, active_container
from registry_service.dispatch import invoke_remote, invoke_tool
from registry_service.principals import ServicePrincipal, UserPrincipal

log = get_logger(__name__)
router = APIRouter()


def _bearer(request: Request) -> str:
    """The bearer token from the Authorization header ('' when absent); X-Auth-* headers are never trusted."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _client_header(request: Request) -> Optional[int]:
    """X-Client-Id as a positive int, or None when absent/invalid (a super admin uses it to act in another client)."""
    raw = request.headers.get(HEADER_CLIENT_ID, "")
    return int(raw) if raw.isdigit() and 0 < int(raw) < 2**31 else None


async def _authenticate(container: Container, request: Request):
    """Return the UserPrincipal or ServicePrincipal for a verified token. Raises AppError (401/403) otherwise."""
    token = _bearer(request)
    if not token:
        raise AppError(Errors.AUTH_TOKEN_MISSING)
    # Prefer a user token (the admin UI); fall back to a service token (an agent/service).
    try:
        claims = await verify_user_token(container.verifier, token)
    except AppError:
        caller = await verify_service_token(container.verifier, token, JWT_AUDIENCE_REGISTRY)  # raises if not valid
        return ServicePrincipal(caller)
    # A valid user token: reject it if the user's permissions changed since it was issued (authz staleness).
    try:
        await container.authz.require_current(claims)
    except AppError:
        raise
    except Exception as e:                        # Redis unreachable during the check: fail closed, retryable
        log_event(log, "factory.authz_unavailable", error_type=type(e).__name__)
        raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=5) from None
    return UserPrincipal(claims)


async def _enforce_rate_limit(container: Container, principal) -> None:
    """Per-principal fixed-window rate limit (app-side hook; Traefik does the edge limit). Best-effort: a limiter
    error never blocks a request."""
    limit = container.settings.rate_limit_per_minute
    if limit <= 0:
        return
    window = int(time.time() // 60)
    key = build_key(Keys.FACTORY_RATE_LIMIT, container.env, principal=digest(principal.label), window=window)
    try:
        count = int(await container.redis.incr(key))
        if count == 1:
            await container.redis.expire(key, ttl(Keys.FACTORY_RATE_LIMIT))
    except Exception:                             # limiter unavailable: do not block (fail open)
        return
    if count > limit:
        log_event(log, "factory.rate_limited", principal=principal.label, count=count)
        raise AppError(Errors.RATE_LIMITED, retry_after=60)


async def _json_body(request: Request) -> dict:
    """Parse the JSON body into a dict (empty body -> {}); a non-object body is a validation error."""
    if not (await request.body()):
        return {}
    payload = await request.json()
    if not isinstance(payload, dict):
        raise AppError(Errors.VALIDATION_FAILED, detail="The request body must be a JSON object.")
    return payload


@router.post(FACTORY_PREFIX + "/{tool}")
async def call_factory_tool(tool: str, request: Request):
    """Authenticate, rate-limit, then run a catalog tool through the shared pipeline. camelCase JSON out."""
    container = active_container()
    principal = await _authenticate(container, request)
    await _enforce_rate_limit(container, principal)
    body = await _json_body(request)
    result = await invoke_tool(container, principal, tool, body, client_header=_client_header(request),
                               front_door="api")
    return JSONResponse(result)


@router.post(CATALOGUE_PREFIX + "/{server}/{remote_tool}")
async def call_catalogue_tool(server: str, remote_tool: str, request: Request):
    """Proxy a tool on a registered MCP server, same auth+policy pipeline (action tool:{server}.{tool})."""
    container = active_container()
    principal = await _authenticate(container, request)
    await _enforce_rate_limit(container, principal)
    body = await _json_body(request)
    result = await invoke_remote(container, principal, server, remote_tool, body,
                                 client_header=_client_header(request))
    return JSONResponse(result)


@router.get(NOTIFICATIONS_PREFIX)
async def list_notifications(request: Request):
    """Convenience read for the admin UI's notifications tab (unread first) - the list_notifications tool."""
    container = active_container()
    principal = await _authenticate(container, request)
    await _enforce_rate_limit(container, principal)
    unread_only = request.query_params.get("unreadOnly", "false").lower() == "true"
    result = await invoke_tool(container, principal, "list_notifications", {"unreadOnly": unread_only},
                               client_header=_client_header(request), front_door="api")
    return JSONResponse(result)
