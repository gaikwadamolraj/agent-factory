from typing import Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from http.cookies import SimpleCookie

from ats_common.constants import COOKIE_ACCESS_TOKEN, HEADER_AUTHORIZATION, HEADER_CSRF
from ats_common.context import RequestContext, current_context, reset_context, set_context
from ats_common.errors import AppError, Errors, send_error
from ats_common.logging import get_logger, log_event
from ats_common.middleware.paths import PathRules, header
from ats_common.security import decode_service_token, decode_user_token

log = get_logger("ats.auth")


def _bearer(scope: Scope) -> str:
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


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _with(scope: Scope, **fields) -> Optional[object]:
    return set_context(RequestContext(**{**current_context().__dict__, **fields}))


class JWTAuthMiddleware:
    """Verifies access tokens on protected paths; puts user id/client id/role into the request context + scope state.

    The token comes from `Authorization: Bearer` (API clients) or the httpOnly access cookie (browser). Requests
    authenticated by cookie must send the X-Requested-With header on state-changing methods (CSRF guard: a
    cross-site form or image cannot set custom headers).
    """

    def __init__(self, app: ASGIApp, secret: str, rules: PathRules, algorithm: str = "HS256",
                 cookie_name: str = COOKIE_ACCESS_TOKEN):
        self.app, self.secret, self.rules, self.algorithm, self.cookie_name = app, secret, rules, algorithm, cookie_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") == "OPTIONS" or not self.rules.applies(scope.get("path", "")):
            return await self.app(scope, receive, send)
        token = _bearer(scope)
        if not token:
            token = cookie(scope, self.cookie_name)
            if token and scope.get("method") not in _SAFE_METHODS and not header(scope, HEADER_CSRF):
                log_event(log, "auth.csrf_rejected", path=scope.get("path"))
                return await send_error(send, Errors.AUTH_FORBIDDEN, detail="Missing X-Requested-With header.")
        if not token:
            return await send_error(send, Errors.AUTH_TOKEN_MISSING, extra_headers={"WWW-Authenticate": "Bearer"})
        try:
            claims = decode_user_token(self.secret, token, self.algorithm)
        except AppError as e:
            log_event(log, "auth.rejected", code=e.spec.code, path=scope.get("path"))
            return await send_error(send, e.spec, extra_headers={"WWW-Authenticate": "Bearer"})

        scope.setdefault("state", {})["user"] = claims
        ctx_token = _with(scope, user_id=claims.user_id, username=claims.username, role=claims.role,
                          client_id=claims.client_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_context(ctx_token)


class ServiceAuthMiddleware:
    """Zero trust between services: every protected call needs a short-lived service token for this audience."""

    def __init__(self, app: ASGIApp, secret: str, audience: str, rules: PathRules, allowed_callers: set[str],
                 algorithm: str = "HS256"):
        self.app, self.secret, self.audience, self.rules = app, secret, audience, rules
        self.allowed_callers, self.algorithm = allowed_callers, algorithm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.rules.applies(scope.get("path", "")):
            return await self.app(scope, receive, send)
        token = _bearer(scope)
        if not token:
            return await send_error(send, Errors.AUTH_TOKEN_MISSING)
        try:
            caller = decode_service_token(self.secret, token, self.audience, self.algorithm)
        except AppError as e:
            log_event(log, "service_auth.rejected", code=e.spec.code)
            return await send_error(send, e.spec)
        if caller not in self.allowed_callers:
            log_event(log, "service_auth.forbidden", caller=caller)
            return await send_error(send, Errors.SERVICE_FORBIDDEN)
        scope.setdefault("state", {})["caller_service"] = caller
        ctx_token = _with(scope, caller_service=caller)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_context(ctx_token)
