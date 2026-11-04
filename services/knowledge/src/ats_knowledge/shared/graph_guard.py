"""Guard for graph calls: per-call timeout + a named circuit breaker; failures become KNW catalog errors.

The API (search, forget) and the worker (extraction) use separate breakers, so a struggling extraction model can
never block searches.
"""
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ats_common.errors import AppError
from ats_common.knowledge.errors import KnowledgeErrors
from ats_common.resilience import BreakerConfig, CallTimeoutError, CircuitOpenError, get_breaker, resilient_call

T = TypeVar("T")
SEARCH_BREAKER = "knowledge.graph"            # API: searches and deletes
EXTRACTION_BREAKER = "knowledge.extraction"   # worker: episode extraction


class GraphGuard:
    def __init__(self, breaker_name: str, failures: int, reset_s: float):
        self._breaker = get_breaker(breaker_name, BreakerConfig(failures, reset_s))

    async def call(self, fn: Callable[[], Awaitable[T]], op: str, timeout_s: float) -> T:
        """Run `fn` with a timeout under the breaker. Raises AppError KNW-TMO-0001 / KNW-UNA-0001."""
        try:
            return await resilient_call(fn, op=op, timeout_s=timeout_s, breaker=self._breaker,
                                        is_failure=lambda e: not isinstance(e, AppError))
        except CallTimeoutError:
            raise AppError(KnowledgeErrors.GRAPH_TIMEOUT) from None
        except CircuitOpenError as e:
            raise AppError(KnowledgeErrors.GRAPH_UNAVAILABLE, retry_after=e.retry_after_s) from None
