"""Timeouts, bounded retries and circuit breakers for every outbound call (LLM, MCP, embeddings, HTTP).

    breaker = get_breaker("llm", BreakerConfig(failure_threshold=5, reset_timeout_s=30))
    result = await resilient_call(lambda: client.call(...), op="llm.chat", timeout_s=120,
                                  retry=RetryPolicy(attempts=2), breaker=breaker)

    @retry(RetryPolicy(attempts=2, retry_if=is_flaky), op="agent.ask")   # same policy as a decorator
    async def ask_once(...): ...

Cancellation (asyncio.CancelledError) is never retried or swallowed, so aborting a request stops the call.
"""
import asyncio
import functools
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Optional, TypeVar

from agent_common.logging import get_logger, log_event

T = TypeVar("T")
log = get_logger(__name__)


class CircuitOpenError(RuntimeError):
    """The breaker is open: calls are rejected until `retry_after_s` has passed."""

    def __init__(self, name: str, retry_after_s: int):
        super().__init__(f"circuit '{name}' is open")
        self.name, self.retry_after_s = name, retry_after_s


class CallTimeoutError(TimeoutError):
    def __init__(self, op: str, timeout_s: float):
        super().__init__(f"{op} timed out after {timeout_s}s")
        self.op, self.timeout_s = op, timeout_s


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 5      # consecutive failures that open the circuit
    reset_timeout_s: float = 30.0   # how long it stays open before one trial call (half-open)


class CircuitBreaker:
    """Closed -> (N consecutive failures) -> Open -> (reset timeout) -> Half-open -> success closes / failure opens."""

    def __init__(self, name: str, config: BreakerConfig = BreakerConfig()):
        self.name, self.config = name, config
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._trial_running = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - self._opened_at >= self.config.reset_timeout_s else "open"

    def before_call(self) -> None:
        state = self.state
        if state == "open" or (state == "half_open" and self._trial_running):
            remaining = self.config.reset_timeout_s - (time.monotonic() - (self._opened_at or 0))
            raise CircuitOpenError(self.name, max(1, int(remaining)))
        if state == "half_open":
            self._trial_running = True

    def on_success(self) -> None:
        if self._opened_at is not None:
            log_event(log, "circuit.closed", breaker=self.name)
        self._failures, self._opened_at, self._trial_running = 0, None, False

    def on_failure(self) -> None:
        self._failures += 1
        self._trial_running = False
        if self._opened_at is not None or self._failures >= self.config.failure_threshold:
            self._opened_at = time.monotonic()
            log_event(log, "circuit.opened", breaker=self.name, failures=self._failures)


_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, config: BreakerConfig = BreakerConfig()) -> CircuitBreaker:
    """One breaker per dependency name, shared across the process."""
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(name, config)
    return _BREAKERS[name]


# LM Studio rejects a reply when a small model writes a malformed tool call ("...does not match the expected
# peg-native format"). It is random, so one fresh attempt after a short backoff usually succeeds.
_LLM_FORMAT_ERROR = "does not match the expected"


def is_llm_format_error(error: BaseException) -> bool:
    return _LLM_FORMAT_ERROR in str(error)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 2                        # total attempts, including the first one
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    retry_on: tuple[type[BaseException], ...] = field(default=(TimeoutError, ConnectionError, OSError))
    retry_if: Optional[Callable[[BaseException], bool]] = None   # extra check for errors not identifiable by type

    def should_retry(self, error: BaseException) -> bool:
        return isinstance(error, self.retry_on) or bool(self.retry_if and self.retry_if(error))

    def delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter."""
        return random.uniform(0, min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1)))


async def resilient_call(
    fn: Callable[[], Awaitable[T]],
    *,
    op: str,
    timeout_s: Optional[float],
    retry: Optional[RetryPolicy] = None,
    breaker: Optional[CircuitBreaker] = None,
    is_failure: Callable[[BaseException], bool] = lambda e: True,
) -> T:
    """Run `fn` with a per-attempt timeout, bounded retries and an optional circuit breaker.

    `is_failure` decides which exceptions count against the breaker (e.g. ignore 4xx client errors).
    """
    policy = retry or RetryPolicy(attempts=1)
    last: Optional[BaseException] = None
    for attempt in range(1, policy.attempts + 1):
        if breaker:
            breaker.before_call()
        try:
            result = await (asyncio.wait_for(fn(), timeout_s) if timeout_s else fn())
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            last = CallTimeoutError(op, timeout_s or 0)
            last.__cause__ = e
        except Exception as e:  # noqa: BLE001 - re-raised below after policy handling
            last = e
        else:
            if breaker:
                breaker.on_success()
            return result

        if breaker and is_failure(last):
            breaker.on_failure()
        retryable = policy.should_retry(last)
        log_event(log, "call.failed", op=op, attempt=attempt, of=policy.attempts, retryable=retryable,
                  error_type=type(last).__name__)
        if not retryable or attempt == policy.attempts:
            raise last
        await asyncio.sleep(policy.delay(attempt))
    raise last  # pragma: no cover


def retry(policy: RetryPolicy, *, op: str, timeout_s: Optional[float] = None):
    """Decorator form of `resilient_call` for async functions: bounded attempts with exponential backoff + jitter.

    Each attempt calls the function again, so anything it builds (clients, agents) is fresh per attempt.
    """
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> T:
            return await resilient_call(lambda: fn(*args, **kwargs), op=op, timeout_s=timeout_s, retry=policy)
        return wrapper
    return decorator
