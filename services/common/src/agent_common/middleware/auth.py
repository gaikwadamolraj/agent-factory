"""Authentication middlewares: user access tokens (APIs) and service tokens (MCP servers).

Both verify ES256 tokens of the auth-service locally with its public keys (`JwksVerifier`), on every request.
Identity comes only from the verified token, never from headers set by a proxy.
"""
from http.cookies import SimpleCookie
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from agent_common.constants import COOKIE_ACCESS_TOKEN, HEADER_AUTHORIZATION, HEADER_CSRF
from agent_common.context import RequestContext, current_context, reset_context, set_context
from agent_common.errors import AppError, Errors, send_error
from agent_common.logging import get_logger, log_event
from agent_common.middleware.paths import PathRules, header
from agent_common.security import JwksVerifier, verify_service_token, verify_user_token

if TYPE_CHECKING:                       # imported for type hints only (agent_common.authz needs the database layer)
    from agent_common.authz import AuthzService

log = get_logger("ats.auth")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def bearer_token(scope: Scope) -> str:
    """Token of an `Authorization: Bearer <token>` header ('' when absent)."""
    scheme, _, token = header(scope, HEADER_AUTHORIZATION).partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def cookie(scope: Scope, name: str) -> str:
    """Value of a request cookie ('' when absent or malformed)."""
    raw = header(scope, "cookie")
    if not raw:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
        return jar[name].value if name in jar else ""
    except Exception:
        return ""


def _with(**fields) -> Optional[object]:
    """Copy of the request context with `fields` replaced; returns the reset token."""
    return set_context(RequestContext(**{**current_context().__dict__, **fields}))


class JWTAuthMiddleware:
    """Verifies user access tokens (aud=ats-api) on protected paths; puts user id/client id/role into the request
    context and `scope["state"]["user"]` (UserClaims).

    The token comes from `Authorization: Bearer` (API clients) or the httpOnly access cookie (browser). Requests
    authenticated by cookie must send the X-Requested-With header on state-changing methods (CSRF guard: a
    cross-site form or image cannot set custom headers).

    With `authz` (a function returning the AuthzService, which exists only after startup), a token issued before
    the user's last permission change is rejected with AUTH-EXP-0002 (retryable: the UI refreshes silently).
    """

    def __init__(self, app: ASGIApp, verifier: JwksVerifier, rules: PathRules, cookie_name: str = COOKIE_ACCESS_TOKEN,
                 authz: Optional[Callable[[], "AuthzService"]] = None):
        self.app, self.verifier, self.rules, self.cookie_name = app, verifier, rules, cookie_name
        self.authz = authz

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") == "OPTIONS" or not self.rules.applies(scope.get("path", "")):
            return await self.app(scope, receive, send)
        token = bearer_token(scope)
        if not token:
            token = cookie(scope, self.cookie_name)
            if token and scope.get("method") not in _SAFE_METHODS and not header(scope, HEADER_CSRF):
                log_event(log, "auth.csrf_rejected", path=scope.get("path"))
                return await send_error(send, Errors.AUTH_FORBIDDEN, detail="Missing X-Requested-With header.")
        if not token:
            return await send_error(send, Errors.AUTH_TOKEN_MISSING, extra_headers=_BEARER_CHALLENGE)
        try:
            claims = await verify_user_token(self.verifier, token)
            if self.authz is not None:
                await self.authz().require_current(claims)
        except AppError as e:
            log_event(log, "auth.rejected", code=e.spec.code, path=scope.get("path"))
            return await send_error(send, e.spec, retry_after=e.retry_after, extra_headers=_BEARER_CHALLENGE)
        except Exception as e:             # Redis unreachable during the version check: fail closed, retryable
            log_event(log, "auth.authz_unavailable", error_type=type(e).__name__)
            return await send_error(send, Errors.UPSTREAM_UNAVAILABLE, retry_after=5)

        scope.setdefault("state", {})["user"] = claims
        ctx_token = _with(user_id=claims.user_id, username=claims.username, role=claims.role,
                          client_id=claims.client_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_context(ctx_token)


class ServiceAuthMiddleware:
    """Zero trust between services: every protected call needs a service token for this audience (the service's
    own name) from an allowed caller. Puts the caller's name into `scope["state"]["caller_service"]`."""

    def __init__(self, app: ASGIApp, verifier: JwksVerifier, audience: str, rules: PathRules,
                 allowed_callers: set[str]):
        self.app, self.verifier, self.audience, self.rules = app, verifier, audience, rules
        self.allowed_callers = allowed_callers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.rules.applies(scope.get("path", "")):
            return await self.app(scope, receive, send)
        token = bearer_token(scope)
        if not token:
            return await send_error(send, Errors.AUTH_TOKEN_MISSING)
        try:
            caller = await verify_service_token(self.verifier, token, self.audience)
        except AppError as e:
            log_event(log, "service_auth.rejected", code=e.spec.code)
            return await send_error(send, e.spec, retry_after=e.retry_after)
        if caller not in self.allowed_callers:
            log_event(log, "service_auth.forbidden", caller=caller)
            return await send_error(send, Errors.SERVICE_FORBIDDEN)
        scope.setdefault("state", {})["caller_service"] = caller
        ctx_token = _with(caller_service=caller)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_context(ctx_token)
