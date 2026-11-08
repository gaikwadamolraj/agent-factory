"""Wiring for the API process (`agent-knowledge-graph`): built once, lazily, so importing never opens connections.
If a store is not reachable yet, the call raises and the next request tries again (failures are not cached)."""
from functools import lru_cache

import redis.asyncio as aioredis
from agent_common.db import init_database

from agent_knowledge_graph.api.answering import Answerer
from agent_knowledge_graph.api.semantic_cache import NoCache, RedisSemanticCache
from agent_knowledge_graph.api.service import KnowledgeService
from agent_knowledge_graph.config import get_settings
from agent_knowledge_graph.shared.graph.graphiti_store import GraphitiStore
from agent_knowledge_graph.shared.graph_guard import SEARCH_BREAKER, GraphGuard
from agent_knowledge_graph.shared.graph_versions import GraphVersions


@lru_cache
def get_service() -> KnowledgeService:
    settings = get_settings()
    if settings.redis_url is None:
        raise RuntimeError("REDIS_URL is required (graph versions and semantic cache).")
    redis_client = aioredis.from_url(settings.redis_connection_url, decode_responses=True)
    cache = RedisSemanticCache(settings) if settings.semantic_cache_enabled else NoCache()
    return KnowledgeService(
        settings, init_database(settings), GraphitiStore(settings), cache,
        GraphVersions(redis_client, settings.app_env), Answerer(),
        GraphGuard(SEARCH_BREAKER, settings.graph_breaker_failures, settings.graph_breaker_reset_s))
