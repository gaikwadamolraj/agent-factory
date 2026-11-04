"""The worker's one job: turn a claimed episode into graph knowledge, then record the outcome.

    claimed row --> split into sections (shared/sections.py)
                --> for each section not done yet: Graphiti.add_episode (LLM extraction into the tenant graph),
                    its graph episode id recorded at once (a retry resumes with the next section)
                    all ok  -> status done, raw text cleared, graph version bumped
                    failure -> back to queued with backoff, or failed after WORKER_MAX_ATTEMPTS
"""
import asyncio
import time

from ats_common.db import Database
from ats_common.errors import AppError
from ats_common.knowledge.errors import KnowledgeErrors
from ats_common.knowledge.ontology import parse_ontology
from ats_common.logging import get_logger, log_event

from ats_knowledge import metrics
from ats_knowledge.config import KnowledgeSettings
from ats_knowledge.shared.episodes import EpisodeRepository, KnowledgeEpisode
from ats_knowledge.shared.graph.store import EpisodeInput, GraphStore
from ats_knowledge.shared.graph_guard import GraphGuard
from ats_knowledge.shared.graph_versions import GraphVersions
from ats_knowledge.shared.ontology_types import extraction_instructions, graphiti_types
from ats_knowledge.shared.sections import split_sections, with_context

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

    async def _record_failure(self, episode: KnowledgeEpisode, error: Exception) -> None:
        code = error.spec.code if isinstance(error, AppError) else KnowledgeErrors.EXTRACTION_FAILED.code
        final = episode.attempts >= self.settings.worker_max_attempts
        retry_in_s = None if final else RETRY_BASE_DELAY_S * 2 ** episode.attempts
        async with self.db.session() as session:
            await EpisodeRepository(session).mark_failed(episode.id, code, retry_in_s)
        metrics.episodes_total.add(1, {"outcome": "failed" if final else "retry"})
        log_event(log, "knowledge.episode_failed", episode_id=episode.id, graph_id=episode.graph_id, code=code,
                  attempt=episode.attempts, final=final, retry_in_s=retry_in_s, error_type=type(error).__name__,
                  error=str(error)[:300])
