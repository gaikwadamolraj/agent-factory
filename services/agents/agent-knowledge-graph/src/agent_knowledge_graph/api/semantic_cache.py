"""Semantic answer cache (RedisVL on Redis 8): a question that means the same as an earlier one, for the same tenant
graph, gets the stored answer without a graph search or an LLM call.

Isolation and freshness come from one exact-match tag, the *scope*:
    scope = hash(graph_id, graph_version, guidance, ontology, answer-mode)
* graph_id carries namespace + client + project, so tenants never share entries.
* graph_version is bumped whenever the graph changes (episode added or forgotten), so old answers stop matching
  immediately; they expire with the TTL.
* guidance/ontology are part of it because they change what a good answer is; so is the answer model identity
  (alias + LLM config version), so a model change never serves answers of the old model.

Vectors come from agent_common.llm (the platform's one embedding model); the question is embedded once per search and
reused for the store after a miss. RedisVL needs no model of its own.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional, Protocol

from agent_common.knowledge.contracts import SearchResult
from agent_common.knowledge.ontology import fingerprint
from agent_common.llm import embed_query, get_llm_settings
from agent_common.logging import get_logger, log_event
from agent_common.redis_keys import Keys, build_key, ttl
from redisvl.extensions.cache.llm import SemanticCache
from redisvl.query.filter import Tag
from redisvl.utils.vectorize import CustomVectorizer

from agent_knowledge_graph.config import KnowledgeSettings

log = get_logger(__name__)
SCOPE_FIELD = "scope"


@dataclass(frozen=True)
class CacheScope:
    graph_id: str
    graph_version: int
    guidance: str
    ontology_id: str
    with_answer: bool
    answer_model: str = ""
    filters: str = ""           # SearchRequest.filter_key(): subject, types, expired (a filtered result is different)

    @property
    def tag(self) -> str:
        return fingerprint(self.graph_id, str(self.graph_version), self.guidance, self.ontology_id,
                           str(self.with_answer), self.answer_model, self.filters)


@dataclass
class CacheLookup:
    """Result of a lookup: the cached result (or None) and the question vector to reuse when storing."""
    result: Optional[SearchResult] = None
    vector: Optional[list[float]] = None


class AnswerCache(Protocol):
    async def lookup(self, scope: CacheScope, question: str) -> CacheLookup: ...
    async def store(self, scope: CacheScope, question: str, lookup: CacheLookup, result: SearchResult) -> None: ...


class RedisSemanticCache:
    """AnswerCache on RedisVL. Failures never break a search: the cache is skipped and the reason logged."""

    def __init__(self, settings: KnowledgeSettings):
        self.settings = settings
        self._cache: Optional[SemanticCache] = None
        self._init_lock = asyncio.Lock()

    def _create(self) -> SemanticCache:
        dims = get_llm_settings().embedding_dim
        return SemanticCache(
            name=build_key(Keys.KNOWLEDGE_SEMANTIC_CACHE, self.settings.app_env),
            distance_threshold=self.settings.semantic_cache_distance,
            ttl=self.settings.semantic_cache_ttl_s or ttl(Keys.KNOWLEDGE_SEMANTIC_CACHE),
            vectorizer=CustomVectorizer(embed=lambda _: [0.0] * dims),   # vectors are always passed in
            filterable_fields=[{"name": SCOPE_FIELD, "type": "tag"}],
            redis_url=self.settings.redis_connection_url)

    async def _get_cache(self) -> SemanticCache:
        async with self._init_lock:
            if self._cache is None:
                self._cache = await asyncio.to_thread(self._create)   # index check/creation is synchronous
            return self._cache

    async def lookup(self, scope: CacheScope, question: str) -> CacheLookup:
        lookup = CacheLookup()
        try:
            cache = await self._get_cache()
            lookup.vector = await embed_query(question)
            hits = await cache.acheck(vector=lookup.vector, num_results=1,
                                      filter_expression=Tag(SCOPE_FIELD) == scope.tag)
            lookup.result = SearchResult.model_validate_json(hits[0]["response"]) if hits else None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(log, "knowledge.cache_unavailable", operation="lookup", error_type=type(e).__name__)
        return lookup

    async def store(self, scope: CacheScope, question: str, lookup: CacheLookup, result: SearchResult) -> None:
        if lookup.vector is None:      # the lookup could not embed: nothing to store under
            return
        try:
            cache = await self._get_cache()
            await cache.astore(prompt=question, response=result.model_dump_json(), vector=lookup.vector,
                               filters={SCOPE_FIELD: scope.tag})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event(log, "knowledge.cache_unavailable", operation="store", error_type=type(e).__name__)


class NoCache:
    """AnswerCache used when SEMANTIC_CACHE_ENABLED=false."""

    async def lookup(self, scope: CacheScope, question: str) -> CacheLookup:
        return CacheLookup()

    async def store(self, scope: CacheScope, question: str, lookup: CacheLookup, result: SearchResult) -> None:
        return None

