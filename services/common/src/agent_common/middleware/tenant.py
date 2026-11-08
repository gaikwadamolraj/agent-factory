"""Tenant check of every tenant request: X-Client-Id / X-Project-Id against the verified token and the user's
cached permissions (agent_common.authz: versioned snapshot in Redis, database only on a miss)."""
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.types import ASGIApp, Receive, Scope, Send

from agent_common.constants import HEADER_CLIENT_ID, HEADER_PROJECT_ID
from agent_common.context import RequestContext, current_context, reset_context, set_context
from agent_common.errors import Errors, send_error
from agent_common.logging import get_logger, log_event
from agent_common.middleware.paths import PathRules, header

if TYPE_CHECKING:                       # imported for type hints only (agent_common.authz needs the database layer)
    from agent_common.authz import AuthzService

log = get_logger("ats.tenant")


def _positive_int(value: str) -> int | None:
    return int(value) if value.isdigit() and 0 < int(value) < 2**31 else None


class TenantMiddleware:
    """Requires X-Client-Id + X-Project-Id on tenant paths: the client must be the token's client (`cid`), and the
    user must be active and a member of that active project (AuthzService snapshot at the user's current version).

    Runs after JWTAuthMiddleware (needs the user claims). Sets client/project on the request context + scope state.
    `authz` returns the AuthzService (created at startup, after the middleware stack is built).
    """

    def __init__(self, app: ASGIApp, rules: PathRules, authz: Callable[[], "AuthzService"]):
        self.app, self.rules, self.authz = app, rules, authz

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") == "OPTIONS" or not self.rules.applies(scope.get("path", "")):
            return await self.app(scope, receive, send)
        claims = scope.get("state", {}).get("user")
        if claims is None:
            return await send_error(send, Errors.AUTH_TOKEN_MISSING)

        client_id = _positive_int(header(scope, HEADER_CLIENT_ID))
        project_id = _positive_int(header(scope, HEADER_PROJECT_ID))
        if client_id is None or project_id is None:
            return await send_error(send, Errors.TENANT_HEADERS_MISSING)
        if client_id != claims.client_id or not await self._may_use_project(claims.user_id, client_id, project_id):
            log_event(log, "tenant.forbidden", requested_client=client_id, requested_project=project_id)
            return await send_error(send, Errors.TENANT_FORBIDDEN)

        scope["state"]["tenant"] = (client_id, project_id)
        token = set_context(RequestContext(**{**current_context().__dict__, "client_id": client_id,
                                              "project_id": project_id}))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_context(token)

    async def _may_use_project(self, user_id: int, client_id: int, project_id: int) -> bool:
        """Is the user active, in this client, and a member of this active project (current authz version)?"""
        snapshot = await self.authz().snapshot(user_id, client_id)
        return (snapshot is not None and snapshot.active and snapshot.client_id == client_id
                and project_id in snapshot.project_ids)
