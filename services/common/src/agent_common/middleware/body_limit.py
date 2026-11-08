from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_common.errors import Errors, send_error
from agent_common.middleware.paths import PathRules, header


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects request bodies above `max_bytes` with VAL-LIM-0001 (413): early via Content-Length, otherwise while
    streaming, so an oversized upload is never fully read or written to disk."""

    def __init__(self, app: ASGIApp, max_bytes: int, rules: PathRules = PathRules()):
        self.app, self.max_bytes, self.rules = app, max_bytes, rules

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.rules.applies(scope.get("path", "")):
            return await self.app(scope, receive, send)
        declared = header(scope, "content-length")
        if declared.isdigit() and int(declared) > self.max_bytes:
            return await send_error(send, Errors.PAYLOAD_TOO_LARGE)

        received, started = 0, False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await send_error(send, Errors.PAYLOAD_TOO_LARGE)
