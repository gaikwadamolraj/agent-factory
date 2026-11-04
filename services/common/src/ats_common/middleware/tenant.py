from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from ats_common.constants import HEADER_CLIENT_ID, HEADER_PROJECT_ID
from ats_common.context import RequestContext, current_context, reset_context, set_context
from ats_common.errors import Errors, send_error
from ats_common.logging import get_logger, log_event
from ats_common.middleware.paths import PathRules, header

log = get_logger("ats.tenant")

# (user_id, client_id, project_id) -> is the user a member of that active project of that client?
MembershipCheck = Callable[[int, int, int], Awaitable[bool]]


def _positive_int(value: str) -> int | None:
    return int(value) if value.isdigit() and 0 < int(value) < 2**31 else None


class TenantMiddleware:
    """Requires X-Client-Id + X-Project-Id on tenant paths, matching the token's client and a project membership.

    Runs after JWTAuthMiddleware (needs the user claims). Sets client/project on the request context + scope state.
    """

    def __init__(self, app: ASGIApp, rules: PathRules, is_member: MembershipCheck):
        self.app, self.rules, self.is_member = app, rules, is_member

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
        if client_id != claims.client_id or not await self.is_member(claims.user_id, client_id, project_id):
            log_event(log, "tenant.forbidden", requested_client=client_id, requested_project=project_id)
            return await send_error(send, Errors.TENANT_FORBIDDEN)

        scope["state"]["tenant"] = (client_id, project_id)
        token = set_context(RequestContext(**{**current_context().__dict__, "client_id": client_id,
                                              "project_id": project_id}))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_context(token)
