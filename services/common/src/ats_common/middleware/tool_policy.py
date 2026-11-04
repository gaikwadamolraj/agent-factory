import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ats_common.errors import Errors, send_error
from ats_common.logging import get_logger, log_event
from ats_common.middleware.paths import PathRules

log = get_logger("ats.tool_policy")
MAX_MCP_BODY_BYTES = 2 * 1024 * 1024


class ToolPolicyMiddleware:
    """Allow-list of MCP tools per calling service. Inspects JSON-RPC `tools/call` requests on MCP paths.

    policy: {"ats-orchestrator": {"screen_candidate", "ask_agent"}, ...}; "*" allows every tool.
    Runs after ServiceAuthMiddleware (needs the caller identity).
    """

    def __init__(self, app: ASGIApp, rules: PathRules, policy: dict[str, set[str]]):
        self.app, self.rules, self.policy = app, rules, policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or not self.rules.applies(scope.get("path", "")):
            return await self.app(scope, receive, send)

        body, more = b"", True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_MCP_BODY_BYTES:
                return await send_error(send, Errors.PAYLOAD_TOO_LARGE)

        caller = scope.get("state", {}).get("caller_service", "")
        allowed = self.policy.get(caller, set())
        for tool in self._called_tools(body):
            if "*" not in allowed and tool not in allowed:
                log_event(log, "tool_policy.denied", caller=caller, tool=tool)
                return await send_error(send, Errors.SERVICE_FORBIDDEN, detail=f"Tool '{tool}' is not allowed.")

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _called_tools(body: bytes) -> list[str]:
        try:
            payload = json.loads(body or b"null")
        except ValueError:
            return []
        messages = payload if isinstance(payload, list) else [payload]
        return [str(m.get("params", {}).get("name", "")) for m in messages
                if isinstance(m, dict) and m.get("method") == "tools/call"]
