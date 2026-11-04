"""API use cases of the knowledge service (process `ats-knowledge`), free of MCP and framework code.

    ingest       validate, de-duplicate, INSERT into the ledger as `queued`. Returns at once; the WORKER extracts.
    get_episode  status of an ingested episode.
    search       semantic cache -> graph search (filters, optional subject scope) -> grounded answer -> cache.
    forget       remove a source's episodes (every section) from the graph and the ledger; graph version bumped.
    update_entity  deterministic rename / attribute fix of one entity; graph version bumped.
    list_episodes  ledger rows of a graph (optionally one subject or source).
    cancel       stop a running search.

Extraction itself is not here: see ats_knowledge.worker.extraction.
"""
import asyncio
import hashlib
import time
from datetime import UTC, datetime
from typing import Optional

from ats_common.db import Database
from ats_common.errors import AppError
from ats_common.knowledge.contracts import (
    EntityHit,
    EpisodeInfo,
    EpisodeList,
    EpisodeStatus,
    ForgetRequest,
    ForgetResult,
    IngestReceipt,
    IngestRequest,
    ListEpisodesRequest,
    SearchRequest,
    SearchResult,
    TenantRef,
    UpdateEntityRequest,
)
from ats_common.knowledge.errors import KnowledgeErrors
from ats_common.knowledge.ontology import Ontology, parse_ontology
from ats_common.llm import Purpose, model_identity
from ats_common.logging import get_logger, log_event
from pydantic import ValidationError

from ats_knowledge import metrics
from ats_knowledge.api.answering import NOT_FOUND, Answerer
from ats_knowledge.api.semantic_cache import AnswerCache, CacheScope
from ats_knowledge.config import KnowledgeSettings
from ats_knowledge.shared.episodes import EpisodeRepository, KnowledgeEpisode
from ats_knowledge.shared.graph.store import GraphQuery, GraphStore
from ats_knowledge.shared.graph_guard import GraphGuard
from ats_knowledge.shared.graph_versions import GraphVersions
from ats_knowledge.shared.tenancy import TenantGraph

log = get_logger(__name__)
FREE_EXTRACTION = "free"


def parse_request_ontology(raw: Optional[dict]) -> Optional[Ontology]:
    """Ontology from a request; invalid -> AppError KNW-INV-0002."""
    try:
        return parse_ontology(raw)
    except (ValidationError, ValueError, KeyError, TypeError):
        raise AppError(KnowledgeErrors.ONTOLOGY_INVALID) from None


def content_hash(text: str, guidance: str, ontology: Optional[Ontology]) -> str:
    """Same text + same instructions = same episode (re-sending it is a no-op)."""
    h = hashlib.sha256()
    for part in (text, guidance, ontology.cache_id if ontology else FREE_EXTRACTION):
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _info(episode: KnowledgeEpisode) -> EpisodeInfo:
    return EpisodeInfo(episode_id=episode.id, source_type=episode.source_type, source_ref=episode.source_ref,
                       status=EpisodeStatus(episode.status), attempts=episode.attempts,
                       error_code=episode.error_code, updated_at=episode.updated_at,
                       subject_ref=episode.subject_ref, sections=len(episode.graph_episode_uuids or []))


class KnowledgeService:
    def __init__(self, settings: KnowledgeSettings, db: Database, store: GraphStore, cache: AnswerCache,
                 versions: GraphVersions, answerer: Answerer, guard: GraphGuard):
        self.settings, self.db, self.store, self.cache = settings, db, store, cache
        self.versions, self.answerer, self.guard = versions, answerer, guard
        self._running: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ ingest
    async def ingest(self, request: IngestRequest) -> IngestReceipt:
        """Queue text for the worker. Identical text + instructions for the same graph is not queued twice."""
        if len(request.text) > self.settings.max_text_chars:
            raise AppError(KnowledgeErrors.TEXT_TOO_LARGE)
        ontology = parse_request_ontology(request.ontology)
        graph_id = TenantGraph.of(request).graph_id
        digest = content_hash(request.text, request.guidance, ontology)
        async with self.db.session() as session:
            episodes = EpisodeRepository(session)
            existing = await episodes.find_by_hash(graph_id, digest)
            if existing and existing.status != EpisodeStatus.FAILED:
                metrics.ingest_requests_total.add(1, {"outcome": "duplicate"})
                return IngestReceipt(episode_id=existing.id, status=EpisodeStatus(existing.status), duplicate=True)
            if existing:   # failed before: give it a fresh set of attempts
                existing.content, existing.status, existing.attempts = request.text, EpisodeStatus.QUEUED, 0
                existing.subject_ref = request.subject_ref or existing.subject_ref
                existing.available_at, existing.error_code = datetime.now(UTC), None
                episode = await episodes.add(existing)
                outcome = "requeued"
            else:
                episode = await episodes.add(KnowledgeEpisode(
                    client_id=request.client_id, project_id=request.project_id, namespace=request.namespace,
                    graph_id=graph_id, source_type=request.source_type, source_ref=request.source_ref,
                    content_hash=digest, content=request.text, guidance=request.guidance,
                    ontology=ontology.model_dump(by_alias=True) if ontology else None,
                    subject_ref=request.subject_ref, reference_time=request.reference_time or datetime.now(UTC)))
                outcome = "queued"
        metrics.ingest_requests_total.add(1, {"outcome": outcome})
        log_event(log, "knowledge.episode_queued", graph_id=graph_id, episode_id=episode.id,
                  source_type=request.source_type, characters=len(request.text))
        return IngestReceipt(episode_id=episode.id, status=EpisodeStatus.QUEUED)

    async def get_episode(self, ref: TenantRef, episode_id: str) -> EpisodeInfo:
        """Status of one episode of this tenant graph. Raises KNW-NFD-0001."""
        async with self.db.session() as session:
            episode = await EpisodeRepository(session).get(TenantGraph.of(ref).graph_id, episode_id)
        if episode is None:
            raise AppError(KnowledgeErrors.EPISODE_NOT_FOUND)
        return _info(episode)

    # ------------------------------------------------------------------ search
    async def search(self, request: SearchRequest) -> SearchResult:
        """Cache -> graph -> grounded answer -> cache. Cancellable through cancel(request_id)."""
        if request.request_id and (task := asyncio.current_task()):
            self._running[request.request_id] = task
        started, cache_label = time.perf_counter(), "miss"
        try:
            ontology = parse_request_ontology(request.ontology)
            graph_id = TenantGraph.of(request).graph_id
            scope = CacheScope(graph_id, await self.versions.current(graph_id), request.guidance,
                               ontology.cache_id if ontology else FREE_EXTRACTION, request.answer,
                               model_identity(Purpose.ANSWER, request.client_id) if request.answer else "",
                               request.filter_key())
            lookup = await self.cache.lookup(scope, request.question)
            if lookup.result:
                cache_label = "hit"
                return lookup.result.model_copy(update={"cached": True})
            result = await self._search_graph(request, graph_id, ontology)
            result.graph_version = scope.graph_version
            if result.facts:          # never cache "nothing found": the graph may be filling right now
                await self.cache.store(scope, request.question, lookup, result)
            return result
        finally:
            self._running.pop(request.request_id, None)
            metrics.searches_total.add(1, {"cache": cache_label})
            metrics.search_duration.record(time.perf_counter() - started, {"cache": cache_label})

    async def _search_graph(self, request: SearchRequest, graph_id: str, ontology: Optional[Ontology]) -> SearchResult:
        """Graph search with the request's filters. A subject scope with nothing extracted yet returns no facts."""
        episode_uuids = None
        if request.subject_refs:
            async with self.db.session() as session:
                episode_uuids = frozenset(await EpisodeRepository(session).done_episode_uuids(
                    graph_id, request.subject_refs))
            if not episode_uuids:
                return SearchResult(answer=NOT_FOUND if request.answer else "")
        labels = request.entity_types or (ontology.entity_names if ontology else [])
        query = GraphQuery(graph_id=graph_id, text=request.question, limit=request.limit, entity_labels=labels,
                           relation_types=request.relation_types, episode_uuids=episode_uuids,
                           include_expired=request.include_expired)
        hits = await self.guard.call(lambda: self.store.search(query), "graph.search",
                                     self.settings.graph_query_timeout_s)
        facts = hits.facts[: self.settings.max_answer_facts]
        result = SearchResult(facts=facts, entities=hits.entities)
        if request.answer and facts:
            result.answer, result.cited_fact_ids = await self.answerer.answer(
                request.question, request.guidance, facts, hits.entities, request.client_id)
        elif request.answer:
            result.answer = NOT_FOUND
        return result

    # ------------------------------------------------------------------ forget / cancel
    async def forget(self, request: ForgetRequest) -> ForgetResult:
        """Delete everything learned from one source: graph episodes (and facts only they support) + ledger rows."""
        graph_id = TenantGraph.of(request).graph_id
        async with self.db.session() as session:
            episodes = list(await EpisodeRepository(session).by_source(graph_id, request.source_ref))
        for uuid in [u for episode in episodes for u in episode.graph_episode_uuids or []]:
            await self.guard.call(lambda u=uuid: self.store.remove_episode(graph_id, u), "graph.remove_episode",
                                  self.settings.graph_query_timeout_s)
        async with self.db.session() as session:
            await EpisodeRepository(session).delete_ids([e.id for e in episodes])
        if episodes:
            await self.versions.bump(graph_id)
        log_event(log, "knowledge.source_forgotten", graph_id=graph_id, episodes=len(episodes))
        return ForgetResult(forgotten=len(episodes))

    async def update_entity(self, request: UpdateEntityRequest) -> EntityHit:
        """Rename / set attributes of one entity of this tenant graph. Raises KNW-NFD-0002 if it is not there."""
        graph_id = TenantGraph.of(request).graph_id
        entity = await self.guard.call(
            lambda: self.store.update_entity(graph_id, request.entity_id, request.name, request.attributes),
            "graph.update_entity", self.settings.graph_query_timeout_s)
        if entity is None:
            raise AppError(KnowledgeErrors.ENTITY_NOT_FOUND)
        await self.versions.bump(graph_id)     # cached answers may quote the old value
        return entity

    async def list_episodes(self, request: ListEpisodesRequest) -> EpisodeList:
        """Ledger rows of this tenant graph, newest first."""
        async with self.db.session() as session:
            rows = await EpisodeRepository(session).recent(TenantGraph.of(request).graph_id, request.subject_ref,
                                                           request.source_ref, request.limit)
        return EpisodeList(episodes=[_info(row) for row in rows])

    def cancel(self, request_id: str) -> bool:
        """Cancel a running search; False if nothing with that id is running."""
        task = self._running.get(request_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True
