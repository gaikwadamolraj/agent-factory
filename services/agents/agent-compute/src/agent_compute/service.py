"""Executes code it is given (written by a calling agent) in the sandbox and reports the outcome as text.

The compute agent does not write or change code: callers own the code, so a caller can read the error text, fix
its program and send it again (bounded by the caller's step budget).
"""
import asyncio
import re
import time
from typing import Optional

from strands.sandbox import ExecutionResult

from agent_common.context import bind
from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from agent_compute.config import ComputeSettings
from agent_compute.errors import ComputeErrors
from agent_compute.sandbox import HttpSandbox

log = get_logger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_PYTHON = {"python", "python3", "py"}


def as_text(result: ExecutionResult) -> str:
    """Plain-text result for the caller; JSON printed by the program stays inside stdout as text."""
    text = f"exit code: {result.exit_code}\nstdout:\n{result.stdout.rstrip() or '(empty)'}"
    if result.stderr.strip():
        text += f"\nstderr:\n{result.stderr.rstrip()}"
    return text


class ComputeService:
    def __init__(self, settings: ComputeSettings):
        self.settings = settings
        self.sandbox = HttpSandbox(settings)
        self._running: dict[str, asyncio.Task] = {}

    def _validate(self, request_id: str, language: str, code: str) -> None:
        if not _REQUEST_ID.match(request_id or "") or language.lower() not in _PYTHON:
            raise AppError(ComputeErrors.INPUT_INVALID)
        if not code.strip() or len(code) > self.settings.max_code_chars:
            raise AppError(ComputeErrors.INPUT_INVALID)

    async def execute(self, request_id: str, language: str, code: str, timeout_s: Optional[float]) -> str:
        self._validate(request_id, language, code)
        bind(compute_request_id=request_id)
        task = asyncio.current_task()
        if task:
            self._running[request_id] = task
        started = time.perf_counter()
        try:
            result = await self.sandbox.execute_code(code, "python", timeout=timeout_s or None)
        except asyncio.CancelledError:
            log_event(log, "compute.cancelled")
            raise
        finally:
            self._running.pop(request_id, None)
        log_event(log, "compute.executed", exit_code=result.exit_code, code_chars=len(code),
                  duration_ms=round((time.perf_counter() - started) * 1000))
        return as_text(result)

    def cancel(self, request_id: str) -> bool:
        task = self._running.get(request_id)
        if task is None or task.done():
            log_event(log, "compute.cancel_not_running", compute_request_id=request_id)
            return False
        task.cancel()
        return True
