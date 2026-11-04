"""Wiring for the API process (`ats-knowledge`): built once, lazily, so importing never opens connections.
If a store is not reachable yet, the call raises and the next request tries again (failures are not cached)."""
from functools import lru_cache

import redis.asyncio as aioredis
from ats_common.db import init_database

from ats_knowledge.api.answering import Answerer
from ats_knowledge.api.semantic_cache import NoCache, RedisSemanticCache
from ats_knowledge.api.service import KnowledgeService
from ats_knowledge.config import get_settings
from ats_knowledge.shared.graph.graphiti_store import GraphitiStore
from ats_knowledge.shared.graph_guard import SEARCH_BREAKER, GraphGuard
from ats_knowledge.shared.graph_versions import GraphVersions


@lru_cache
def get_service() -> KnowledgeService:
    settings = get_settings()
    if settings.redis_url is None:
        raise RuntimeError("REDIS_URL is required (graph versions and semantic cache).")
    redis_client = aioredis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    cache = RedisSemanticCache(settings) if settings.semantic_cache_enabled else NoCache()
    return KnowledgeService(
        settings, init_database(settings), GraphitiStore(settings), cache,
        GraphVersions(redis_client, settings.app_env), Answerer(),
        GraphGuard(SEARCH_BREAKER, settings.graph_breaker_failures, settings.graph_breaker_reset_s))
