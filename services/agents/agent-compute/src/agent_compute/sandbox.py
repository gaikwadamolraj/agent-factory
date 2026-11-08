"""Code sandboxes, implemented against the Strands `Sandbox` interface so the agent never depends on where code runs.

* `HttpSandbox`: our agent-sandbox pod (services/agent-sandbox): Python only, no network, no secrets, strict limits.
* Later: an NVIDIA OpenShell implementation of the same interface can replace it without touching the agent.

Only code execution is offered. Shell commands and file access are refused on purpose: the agent needs neither.
"""
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from strands.sandbox import ExecutionResult, Sandbox, StreamChunk
from strands.sandbox.types import FileInfo

from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from agent_common.resilience import (BreakerConfig, CallTimeoutError, CircuitOpenError, RetryPolicy, get_breaker,
                                   resilient_call)
from agent_compute.config import ComputeSettings
from agent_compute.errors import ComputeErrors

log = get_logger(__name__)
_REFUSED = "This sandbox only runs Python code (execute_code); shell commands and file access are disabled."


class SandboxBusyError(ConnectionError):
    """The sandbox has no free slot (HTTP 429); retried with backoff."""


class HttpSandbox(Sandbox):
    def __init__(self, settings: ComputeSettings):
        self.settings = settings
        self.breaker = get_breaker("sandbox", BreakerConfig(settings.sandbox_breaker_failures,
                                                            settings.sandbox_breaker_reset_s))

    async def _post(self, code: str, timeout_s: float) -> dict:
        async with httpx.AsyncClient(base_url=self.settings.sandbox_url, timeout=timeout_s + 5) as client:
            response = await client.post("/v1/run", json={"language": "python", "code": code, "timeoutS": timeout_s})
        if response.status_code == 429:
            raise SandboxBusyError("sandbox busy")
        response.raise_for_status()
        return response.json()

    async def execute_code_streaming(self, code: str, language: str, *, timeout: float | None = None,
                                     cwd: str | None = None, env: dict[str, str] | None = None,
                                     **kwargs: Any) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        if language.lower() not in ("python", "python3", "py"):
            yield ExecutionResult(exit_code=2, stdout="", stderr=f"Unsupported language '{language}'. Use python.")
            return
        run_timeout = min(timeout or self.settings.sandbox_run_timeout_s, self.settings.sandbox_max_timeout_s)
        try:
            data = await resilient_call(lambda: self._post(code, run_timeout), op="sandbox.run",
                                        timeout_s=run_timeout + 10, breaker=self.breaker,
                                        retry=RetryPolicy(attempts=3, base_delay_s=0.5, max_delay_s=2.0,
                                                          retry_on=(SandboxBusyError, httpx.TransportError)))
        except (CallTimeoutError, CircuitOpenError, httpx.HTTPError, ConnectionError) as e:
            log_event(log, "sandbox.unavailable", error_type=type(e).__name__)
            raise AppError(ComputeErrors.SANDBOX_UNAVAILABLE,
                           retry_after=getattr(e, "retry_after_s", None) or 10) from None
        if data.get("stdout"):
            yield StreamChunk(data=data["stdout"], stream_type="stdout")
        if data.get("stderr"):
            yield StreamChunk(data=data["stderr"], stream_type="stderr")
        yield ExecutionResult(exit_code=int(data.get("exitCode", 1)), stdout=data.get("stdout", ""),
                              stderr=data.get("stderr", ""))

    async def execute_streaming(self, command: str, *, timeout: float | None = None, cwd: str | None = None,
                                env: dict[str, str] | None = None,
                                **kwargs: Any) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        yield ExecutionResult(exit_code=126, stdout="", stderr=_REFUSED)

    async def read_file(self, path: str, **kwargs: Any) -> bytes:
        raise PermissionError(_REFUSED)

    async def write_file(self, path: str, content: bytes, **kwargs: Any) -> None:
        raise PermissionError(_REFUSED)

    async def remove_file(self, path: str, **kwargs: Any) -> None:
        raise PermissionError(_REFUSED)

    async def list_files(self, path: str, **kwargs: Any) -> list[FileInfo]:
        raise PermissionError(_REFUSED)
