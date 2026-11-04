"""Clients for the MCP agent services (screening agent, compute agent), as LangChain tools from their MCP servers.

Zero trust: every MCP session carries a short-lived service token for that service's audience. Every call has a
timeout and goes through the service's circuit breaker. Errors arrive as "CODE: message"; known codes are re-raised
with the same catalog spec, anything else becomes the service's "unavailable" code.
"""
import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.callbacks import CallbackContext, Callbacks
from langchain_mcp_adapters.tools import load_mcp_tools

from ats_common.constants import JWT_AUDIENCE_AGENT, JWT_AUDIENCE_COMPUTE
from ats_common.errors import AppError, ErrorSpec, get_spec
from ats_common.logging import get_logger, log_event
from ats_common.resilience import (BreakerConfig, CallTimeoutError, CircuitOpenError, RetryPolicy, get_breaker,
                                   resilient_call)
from ats_common.security import create_service_token
from orchestrator import metrics
from orchestrator.config import OrchestratorSettings
from orchestrator.constants import SERVICE_NAME_FOR_AGENT
from orchestrator.errors import OrchErrors

log = get_logger(__name__)
ProgressFn = Callable[[str, str, float], Awaitable[None]]  # (step, message, fraction 0..1)
_CODE_IN_TEXT = re.compile(r"\b([A-Z]{2,5}-[A-Z]{3}-\d{4})\b")


@dataclass(frozen=True)
class AgentTenant:
    client_id: int
    project_id: int
    user_id: int

    def args(self) -> dict:
        return {"clientId": self.client_id, "projectId": self.project_id, "userId": self.user_id}


def _text(output: Any) -> str:
    """MCP tool output -> text (str, list of content blocks, or (content, artifact))."""
    if isinstance(output, tuple):
        output = output[0]
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", str(b)) for b in output)
    return str(output)


class _ServiceTokenAuth(httpx.Auth):
    """Mints a fresh short-lived service token for every HTTP request of an MCP session.

    A screening can run longer than the token TTL; a token minted once at session start would expire mid-session and
    the session's later requests (result, terminate) would be rejected with AUTH-EXP-0001, losing a finished result.
    """

    def __init__(self, mint: Callable[[], str]):
        self._mint = mint

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._mint()}"
        yield request


@dataclass(frozen=True)
class McpServiceTarget:
    """Where and how to reach one MCP service."""
    name: str                     # metric/log label and breaker name, e.g. "agent"
    url: str
    audience: str                 # JWT audience the service accepts
    connect_timeout_s: float
    read_timeout_s: float
    breaker: BreakerConfig
    timeout_error: ErrorSpec
    unavailable_error: ErrorSpec


class McpServiceClient:
    def __init__(self, settings: OrchestratorSettings, target: McpServiceTarget):
        self.settings, self.target = settings, target
        self.breaker = get_breaker(target.name, target.breaker)

    def _token(self) -> str:
        return create_service_token(self.settings.require_jwt_secret(), SERVICE_NAME_FOR_AGENT, self.target.audience,
                                    self.settings.jwt_algorithm)

    def _connection(self) -> dict:
        return {"transport": "streamable_http", "url": self.target.url,
                "auth": _ServiceTokenAuth(self._token),
                "timeout": timedelta(seconds=self.target.connect_timeout_s),
                "sse_read_timeout": timedelta(seconds=self.target.read_timeout_s)}

    async def _tools(self, progress: Optional[ProgressFn]) -> dict[str, BaseTool]:
        async def on_progress(value: float, total: float | None, message: str | None, _: CallbackContext) -> None:
            if progress and message:
                step, _, text = message.partition("|")
                await progress(step, text or step, (value / total) if total else 0.0)

        tools = await load_mcp_tools(None, connection=self._connection(), callbacks=Callbacks(on_progress=on_progress))
        return {t.name: t for t in tools}

    async def _call(self, tool: str, args: dict, timeout_s: float, progress: Optional[ProgressFn] = None) -> str:
        started, outcome, label = time.perf_counter(), "ok", f"{self.target.name}.{tool}"

        async def invoke() -> str:
            tools = await self._tools(progress)
            return _text(await tools[tool].ainvoke(args))

        try:
            return await resilient_call(invoke, op=label, timeout_s=timeout_s,
                                        retry=RetryPolicy(attempts=1), breaker=self.breaker,
                                        is_failure=lambda e: not isinstance(e, AppError))
        except CallTimeoutError:
            outcome = "timeout"
            raise AppError(self.target.timeout_error)
        except CircuitOpenError as e:
            outcome = "circuit_open"
            raise AppError(self.target.unavailable_error, retry_after=e.retry_after_s)
        except AppError:
            outcome = "error"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as e:
            outcome = "error"
            match = _CODE_IN_TEXT.search(str(e))
            spec = get_spec(match.group(1)) if match else None
            code = spec.code if spec else (match.group(1) if match else None)
            log_event(log, "agent.call_failed", tool=label, code=code, error_type=type(e).__name__)
            raise AppError(spec or self.target.unavailable_error, retry_after=None if spec else 10)
        finally:
            metrics.agent_calls_total.add(1, {"tool": label, "outcome": outcome})
            log_event(log, "agent.call", tool=label, outcome=outcome,
                      duration_ms=round((time.perf_counter() - started) * 1000))


class AgentClient(McpServiceClient):
    """The screening agent (ats-agent)."""

    def __init__(self, settings: OrchestratorSettings):
        super().__init__(settings, McpServiceTarget(
            name="agent", url=settings.agent_mcp_url, audience=JWT_AUDIENCE_AGENT,
            connect_timeout_s=settings.agent_connect_timeout_s, read_timeout_s=settings.agent_timeout_s,
            breaker=BreakerConfig(settings.agent_breaker_failures, settings.agent_breaker_reset_s),
            timeout_error=OrchErrors.AGENT_TIMEOUT, unavailable_error=OrchErrors.AGENT_UNAVAILABLE))

    async def screen(self, tenant: AgentTenant, request_id: str, chat_id: str, job_description: str,
                     resume_text: str, progress: Optional[ProgressFn] = None) -> dict:
        """Run a screening; returns the camelCase ScreeningResult dict."""
        text = await self._call("screen_candidate", {
            **tenant.args(), "requestId": request_id, "chatId": chat_id, "jobDescription": job_description,
            "resumeText": resume_text, "skillVersion": self.settings.skill_version,
        }, self.settings.agent_timeout_s, progress)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise AppError(OrchErrors.AGENT_UNAVAILABLE)

    async def ask(self, tenant: AgentTenant, request_id: str, question: str) -> str:
        return await self._call("ask_agent", {**tenant.args(), "requestId": request_id, "question": question},
                                self.settings.agent_timeout_s)

    async def cancel(self, request_id: str) -> bool:
        """Stop a running agent request (and its LLM call). Never raises; returns whether something was cancelled."""
        try:
            text = await self._call("cancel_request", {"requestId": request_id}, self.settings.agent_connect_timeout_s)
            return bool(json.loads(text).get("cancelled"))
        except Exception:
            log_event(log, "agent.cancel_failed", agent_request_id=request_id)
            return False


class ComputeClient(McpServiceClient):
    """The compute agent (ats-compute): runs code written by the caller and returns the output as text."""

    def __init__(self, settings: OrchestratorSettings):
        super().__init__(settings, McpServiceTarget(
            name="compute", url=settings.compute_mcp_url, audience=JWT_AUDIENCE_COMPUTE,
            connect_timeout_s=settings.agent_connect_timeout_s, read_timeout_s=settings.compute_timeout_s,
            breaker=BreakerConfig(settings.agent_breaker_failures, settings.agent_breaker_reset_s),
            timeout_error=OrchErrors.COMPUTE_TIMEOUT, unavailable_error=OrchErrors.COMPUTE_UNAVAILABLE))

    async def execute(self, request_id: str, code: str) -> str:
        """Run Python in the sandbox; returns "exit code: N", stdout and stderr as text."""
        return await self._call("execute_code", {"requestId": request_id, "code": code, "language": "python"},
                                self.settings.compute_timeout_s)
