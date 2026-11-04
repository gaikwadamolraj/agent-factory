"""The worker loop: claim queued episodes from Postgres and extract them, a few at a time.

This file controls work INSIDE one pod. It does not add pods: the number of worker pods is set by Kubernetes
(KEDA scales them on the queue length, see deploy/k8s/keda). Many pods are safe because claiming uses
`FOR UPDATE SKIP LOCKED`: a row locked by one pod is invisible to the others.

    every WORKER_POLL_INTERVAL_S:
        free slots = WORKER_CONCURRENCY - running
        claim up to <free slots> due rows (and lease them for WORKER_LEASE_S)
        start one task per row -> EpisodeExtractor.process
    on SIGTERM: stop claiming, let running tasks finish for WORKER_DRAIN_S, release the rest to the queue, exit
"""
import asyncio
from typing import Optional

from ats_common.logging import get_logger, log_event

from ats_knowledge.config import KnowledgeSettings
from ats_knowledge.shared.episodes import EpisodeRepository, KnowledgeEpisode
from ats_knowledge.worker.container import get_extractor
from ats_knowledge.worker.extraction import EpisodeExtractor

log = get_logger("ats_knowledge.worker")


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


async def wait_for_extractor(stop: asyncio.Event, max_delay_s: float = 30.0) -> Optional[EpisodeExtractor]:
    """Build the extractor once its stores accept connections (FalkorDB may start after the worker on a fresh
    deploy). Retries with exponential backoff instead of crashing; None if stopped while waiting."""
    delay = 1.0
    while not stop.is_set():
        try:
            return get_extractor()
        except Exception as e:
            log_event(log, "knowledge.worker_waiting_for_stores", retry_in_s=delay, error_type=type(e).__name__)
            await _sleep_unless_stopped(stop, delay)
            delay = min(delay * 2, max_delay_s)
    return None


async def _claim(extractor: EpisodeExtractor, limit: int, lease_s: int) -> list[KnowledgeEpisode]:
    try:
        async with extractor.db.session() as session:
            return list(await EpisodeRepository(session).claim(limit, lease_s))
    except Exception as e:   # database briefly unavailable: keep polling
        log_event(log, "knowledge.worker_claim_failed", error_type=type(e).__name__)
        return []


async def run_worker(settings: KnowledgeSettings, stop: asyncio.Event) -> None:
    """Poll -> claim free slots -> process in background tasks, until `stop` is set; then drain."""
    extractor = await wait_for_extractor(stop)
    if extractor is None:
        return
    running: set[asyncio.Task] = set()
    log_event(log, "knowledge.worker_started", concurrency=settings.worker_concurrency)

    while not stop.is_set():
        free = settings.worker_concurrency - len(running)
        if free <= 0:                                    # all slots busy: wait for one to finish
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            continue
        claimed = await _claim(extractor, free, settings.worker_lease_s)
        for episode in claimed:
            task = asyncio.create_task(extractor.process(episode))
            running.add(task)
            task.add_done_callback(running.discard)
        if len(claimed) < free:                          # queue empty for now
            await _sleep_unless_stopped(stop, settings.worker_poll_interval_s)

    if running:   # finish what fits in the drain window; give the rest back to the queue right away
        log_event(log, "knowledge.worker_draining", running=len(running), drain_s=settings.worker_drain_s)
        _, unfinished = await asyncio.wait(running, timeout=settings.worker_drain_s)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)
    await extractor.store.close()
    log_event(log, "knowledge.worker_stopped")
