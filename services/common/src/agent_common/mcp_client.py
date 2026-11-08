"""Call a tool on an internal MCP service (zero trust, resilient). Needs the optional extra `agent-common[mcp]`.

    target = McpTarget(name="knowledge", url="http://agent-knowledge-graph/mcp", audience=JWT_AUDIENCE_KNOWLEDGE_GRAPH,
                       timeout_s=60, unavailable=KnowledgeErrors.SERVICE_UNAVAILABLE,
                       timeout=KnowledgeErrors.GRAPH_TIMEOUT)
    text = await McpToolClient(target, service_tokens).call("search_knowledge", {...})

* Every call opens a short session with a service token for the target (`aud` = the target service) from the
  auth-service (`ServiceTokenProvider`, cached until ~80% of its lifetime).
* Timeout, bounded retry (transport errors only) and a circuit breaker named after the target.
* Tool errors arrive as "CODE: message hint"; a known catalog code is re-raised as the same AppError, anything else
  becomes the target's `unavailable` error. Internal exception text never leaves this module.
"""
import asyncio
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from agent_common.errors import AppError, ErrorSpec, get_spec
from agent_common.logging import get_logger, log_event
from agent_common.resilience import (
    BreakerConfig,
    CallTimeoutError,
    CircuitOpenError,
    RetryPolicy,
    get_breaker,
    resilient_call,
)
from agent_common.security import ServiceTokenProvider

log = get_logger(__name__)
_CODE_IN_TEXT = re.compile(r"\b([A-Z]{2,5}-[A-Z]{3}-\d{4})\b")
_TRANSPORT_ERRORS = (ConnectionError, OSError)   # plus the HTTP library's TransportError, added below


# The MCP SDK changed its client API in 2.x; callers on LangChain's adapters still need 1.x. Support both, so the
# same client works in every service whatever SDK major it is pinned to.
try:  # mcp >= 2
    import httpx2
    from mcp.client.client import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    _TRANSPORT_ERRORS += (httpx2.TransportError,)

    async def _call_tool(url: str, headers: dict, timeout_s: float, connect_s: float, tool: str, args: dict):
        http = create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(timeout_s, connect=connect_s))
        async with http, Client(streamable_http_client(url, http_client=http), read_timeout_seconds=timeout_s) as c:
            return await c.call_tool(tool, args)
except ImportError:  # mcp 1.x
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    _TRANSPORT_ERRORS += (httpx.TransportError,)

    async def _call_tool(url: str, headers: dict, timeout_s: float, connect_s: float, tool: str, args: dict):
        async with streamablehttp_client(url, headers=headers, timeout=timedelta(seconds=connect_s),
                                         sse_read_timeout=timedelta(seconds=timeout_s)) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, args)


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


class McpToolError(RuntimeError):
    """The tool ran and reported an error (is_error=true). Carries the tool's text, never a stack trace."""


@dataclass(frozen=True)
class McpTarget:
    """Where an MCP service lives and how calls to it behave."""

    name: str                       # breaker + log label, e.g. "knowledge"
    url: str
    audience: str                   # JWT audience the service accepts (its service name)
    timeout_s: float
    unavailable: ErrorSpec
    timeout: ErrorSpec
    connect_timeout_s: float = 10.0
    retry_attempts: int = 2
    breaker: BreakerConfig = field(default_factory=BreakerConfig)


class McpToolClient:
    """Thin, reusable client for one MCP service. `tokens` provides this service's tokens for the target."""

    def __init__(self, target: McpTarget, tokens: ServiceTokenProvider):
        self.target, self._tokens = target, tokens
        self._breaker = get_breaker(f"mcp.{target.name}", target.breaker)

    async def _call_once(self, tool: str, arguments: dict[str, Any]) -> str:
        token = await self._tokens.token(self.target.audience)
        result = await _call_tool(self.target.url, {"Authorization": f"Bearer {token}"}, self.target.timeout_s,
                                  self.target.connect_timeout_s, tool, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        if _is_error(result):
            raise McpToolError(text)
        return text

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        """Call `tool` and return its text result. Raises AppError (catalog code) on any failure."""
        op = f"{self.target.name}.{tool}"
        try:
            return await resilient_call(
                lambda: self._call_once(tool, arguments), op=op, timeout_s=self.target.timeout_s,
                retry=RetryPolicy(attempts=self.target.retry_attempts, retry_on=_TRANSPORT_ERRORS),
                breaker=self._breaker, is_failure=lambda e: not isinstance(e, (McpToolError, AppError)))
        except (asyncio.CancelledError, AppError):     # AppError: no service token (auth-service refused or down)
            raise
        except CallTimeoutError:
            raise AppError(self.target.timeout) from None
        except CircuitOpenError as e:
            raise AppError(self.target.unavailable, retry_after=e.retry_after_s) from None
        except McpToolError as e:
            match = _CODE_IN_TEXT.search(str(e))
            spec = get_spec(match.group(1)) if match else None
            log_event(log, "mcp.tool_error", tool=op, code=spec.code if spec else None)
            raise AppError(spec or self.target.unavailable) from None
        except Exception as e:  # transport failure after retries: hide internals, keep the type for the logs
            log_event(log, "mcp.call_failed", tool=op, error_type=type(e).__name__)
            raise AppError(self.target.unavailable, retry_after=10) from None
