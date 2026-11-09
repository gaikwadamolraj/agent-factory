"""The worker's one job: turn a claimed episode into graph knowledge, then record the outcome.

    claimed row --> split into sections (shared/sections.py)
                --> for each section not done yet: Graphiti.add_episode (LLM extraction into the tenant graph),
                    its graph episode id recorded at once (a retry resumes with the next section)
                    all ok  -> status done, raw text cleared, graph version bumped
                    failure -> back to queued with backoff, or failed after WORKER_MAX_ATTEMPTS

Both end states clear the raw text. Re-ingesting the same document restores it (api/service.py re-queues a
failed episode with the text), but a row put back on the queue any other way has nothing left to extract: the
worker fails it with KNW-NFD-0003 rather than reporting a success that left the graph empty.
"""
import asyncio
import time

from agent_common.db import Database
from agent_common.errors import AppError
from agent_common.knowledge.errors import KnowledgeErrors
from agent_common.knowledge.ontology import parse_ontology
from agent_common.logging import get_logger, log_event

from agent_knowledge_graph import metrics
from agent_knowledge_graph.config import KnowledgeSettings
from agent_knowledge_graph.shared.episodes import EpisodeRepository, KnowledgeEpisode
from agent_knowledge_graph.shared.graph.store import EpisodeInput, GraphStore
from agent_knowledge_graph.shared.graph_guard import GraphGuard
from agent_knowledge_graph.shared.graph_versions import GraphVersions
from agent_knowledge_graph.shared.ontology_types import extraction_instructions, graphiti_types
from agent_knowledge_graph.shared.sections import split_sections, with_context

log = get_logger(__name__)
RETRY_BASE_DELAY_S = 30.0     # retry after 60 s, 120 s, ... (doubles per attempt)


class EpisodeExtractor:
    def __init__(self, settings: KnowledgeSettings, db: Database, store: GraphStore, versions: GraphVersions,
                 guard: GraphGuard):
        self.settings, self.db, self.store, self.versions, self.guard = settings, db, store, versions, guard

    async def process(self, episode: KnowledgeEpisode) -> None:
        """Extract one claimed episode section by section and record done / retry / failed.
        Never raises, except on cancellation (then the episode is released for another worker)."""
        started = time.perf_counter()
        inputs = self.section_inputs(episode)
        done = len(episode.graph_episode_uuids or [])
        if not inputs and not done:
            # Nothing to extract. Finishing here would record `done` for an episode that put nothing in the
            # graph - a success no one can tell from a real one. Re-ingesting restores the text (api/service.py),
            # so this means the row was queued some other way, by hand or by a repair script, after `content` was
            # cleared. No retry can bring the text back, so it fails for good rather than looping.
            await self._record_failure(episode, AppError(KnowledgeErrors.EPISODE_TEXT_GONE), final=True)
            return
        try:
            for section in inputs[done:]:
                uuid = await self.guard.call(lambda s=section: self.store.add_episode(s), "graph.add_episode",
                                             self.settings.graph_timeout_s)
                async with self.db.session() as session:
                    await EpisodeRepository(session).add_section(episode.id, uuid)
        except asyncio.CancelledError:   # worker shutting down: give the episode back instead of waiting for the lease
            await asyncio.shield(self._release(episode))
            raise
        except Exception as e:
            await self._record_failure(episode, e)
            return
        async with self.db.session() as session:
            await EpisodeRepository(session).mark_done(episode.id)
        await self.versions.bump(episode.graph_id)
        metrics.episodes_total.add(1, {"outcome": "done"})
        metrics.extraction_duration.record(time.perf_counter() - started)
        log_event(log, "knowledge.episode_done", episode_id=episode.id, graph_id=episode.graph_id,
                  sections=len(inputs), resumed_after=done, duration_ms=round((time.perf_counter() - started) * 1000))

    def section_inputs(self, episode: KnowledgeEpisode) -> list[EpisodeInput]:
        """One EpisodeInput per section of the episode text (deterministic, so a retry gets the same sections)."""
        ontology = parse_ontology(episode.ontology)
        label = f"{episode.source_type} {episode.source_ref}"
        sections = with_context(split_sections(episode.content or "", self.settings.episode_max_chars), label)
        instructions = extraction_instructions(ontology, episode.guidance)
        types = graphiti_types(ontology)
        return [EpisodeInput(graph_id=episode.graph_id, name=f"{label} #{i}", text=text,
                             source_description=episode.source_type, reference_time=episode.reference_time,
                             instructions=instructions, types=types)
                for i, text in enumerate(sections, start=1)]

    async def _release(self, episode: KnowledgeEpisode) -> None:
        async with self.db.session() as session:
            await EpisodeRepository(session).release(episode.id)
        log_event(log, "knowledge.episode_released", episode_id=episode.id, graph_id=episode.graph_id)

    async def _record_failure(self, episode: KnowledgeEpisode, error: Exception, final: bool = False) -> None:
        """Record one failed attempt: back on the queue with a growing delay, or FAILED for good.

        `final` forces the second even on the first attempt, for failures no retry can fix.
        """
        code = error.spec.code if isinstance(error, AppError) else KnowledgeErrors.EXTRACTION_FAILED.code
        final = final or episode.attempts >= self.settings.worker_max_attempts
        retry_in_s = None if final else RETRY_BASE_DELAY_S * 2 ** episode.attempts
        async with self.db.session() as session:
            await EpisodeRepository(session).mark_failed(episode.id, code, retry_in_s)
        metrics.episodes_total.add(1, {"outcome": "failed" if final else "retry"})
        log_event(log, "knowledge.episode_failed", episode_id=episode.id, graph_id=episode.graph_id, code=code,
                  attempt=episode.attempts, final=final, retry_in_s=retry_in_s, error_type=type(error).__name__,
                  error=str(error)[:300])
