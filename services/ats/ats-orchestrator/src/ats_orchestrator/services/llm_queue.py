"""FIFO queue in front of the LLM server: at most N turns run at once (match LM Studio's --parallel), the rest wait
and see their position. One queue per process: run one replica / one uvicorn worker per LM Studio host."""
import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Optional


class LLMRequestQueue:
    def __init__(self, max_concurrent: int):
        self.max_concurrent = max(1, max_concurrent)
        self._active = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def _release(self) -> None:
        while self._waiters:  # hand the slot straight to the next live waiter
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self._active -= 1

    @asynccontextmanager
    async def slot(self, on_position: Optional[Callable[[int], None]] = None) -> AsyncIterator[None]:
        if self._active < self.max_concurrent and not self._waiters:
            self._active += 1
        else:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            last = 0
            try:
                while not waiter.done():
                    position = self._waiters.index(waiter) + 1
                    if on_position and position != last:
                        on_position(position)
                        last = position
                    await asyncio.wait({waiter}, timeout=1.0)
            except BaseException:
                if waiter.done() and not waiter.cancelled():
                    self._release()
                else:
                    waiter.cancel()
                    if waiter in self._waiters:
                        self._waiters.remove(waiter)
                raise
        try:
            yield
        finally:
            self._release()


_queue: Optional[LLMRequestQueue] = None


def get_llm_queue(max_concurrent: int) -> LLMRequestQueue:
    global _queue
    if _queue is None:
        _queue = LLMRequestQueue(max_concurrent)
    return _queue
