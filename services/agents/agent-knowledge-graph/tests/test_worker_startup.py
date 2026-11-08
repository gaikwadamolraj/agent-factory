"""Worker start-up: waits for unreachable stores instead of crashing, and stops cleanly while waiting."""
import asyncio

import agent_knowledge_graph.worker.loop as worker


async def test_waits_until_stores_are_reachable(monkeypatch):
    attempts = []

    def flaky_extractor():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("Connection refused")
        return "extractor"

    monkeypatch.setattr(worker, "get_extractor", flaky_extractor)
    assert await worker.wait_for_extractor(asyncio.Event(), max_delay_s=0.01) == "extractor"
    assert len(attempts) == 3


async def test_stop_while_waiting_returns_none(monkeypatch):
    def unreachable():
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(worker, "get_extractor", unreachable)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    assert await worker.wait_for_extractor(stop, max_delay_s=0.01) is None
