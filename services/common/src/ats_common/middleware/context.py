import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ats_common.constants import HEADER_REQUEST_ID, HEADER_SERVED_BY, HEALTH_PATHS, MAX_REQUEST_ID_LENGTH
from ats_common.context import RequestContext, reset_context, set_context
from ats_common.logging import get_logger, log_event
from ats_common.middleware.paths import header

log = get_logger("ats.access")
_REQUEST_ID = re.compile(rf"^[A-Za-z0-9._-]{{8,{MAX_REQUEST_ID_LENGTH}}}$")


class RequestContextMiddleware:
    """Creates the request context (request id) for logs/spans/errors and writes one access log line per request."""

    def __init__(self, app: ASGIApp, service_name: str, expose_served_by: bool):
        self.app, self.service_name, self.expose_served_by = app, service_name, expose_served_by

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = header(scope, HEADER_REQUEST_ID)
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = set_context(RequestContext(request_id=request_id))
        started, status = time.perf_counter(), 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((HEADER_REQUEST_ID.lower().encode(), request_id.encode()))
                if self.expose_served_by:
                    headers.append((HEADER_SERVED_BY.lower().encode(), self.service_name.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope.get("path") not in HEALTH_PATHS:
                log_event(log, "http.request", method=scope.get("method"), path=scope.get("path"), status=status,
                          duration_ms=round((time.perf_counter() - started) * 1000, 1))
            reset_context(token)


class SecurityHeadersMiddleware:
    """Baseline browser security headers; API responses are never cached."""

    HEADERS = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
               (b"referrer-policy", b"no-referrer")]

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if h[0] != b"server"] + self.HEADERS
                if scope.get("path", "").startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
