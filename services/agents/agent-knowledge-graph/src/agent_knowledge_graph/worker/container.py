"""Wiring for the worker process (`agent-knowledge-graph-worker`). Needs only the database, the graph store and the
graph-version counter - no semantic cache, no answer model."""
from functools import lru_cache

import redis.asyncio as aioredis
from agent_common.db import init_database

from agent_knowledge_graph.config import get_settings
from agent_knowledge_graph.shared.graph.graphiti_store import GraphitiStore
from agent_knowledge_graph.shared.graph_guard import EXTRACTION_BREAKER, GraphGuard
from agent_knowledge_graph.shared.graph_versions import GraphVersions
from agent_knowledge_graph.worker.extraction import EpisodeExtractor


@lru_cache
def get_extractor() -> EpisodeExtractor:
    """Raises if a store is unreachable (e.g. FalkorDB still starting); not cached on failure, so callers retry."""
    settings = get_settings()
    if settings.redis_url is None:
        raise RuntimeError("REDIS_URL is required (graph versions).")
    redis_client = aioredis.from_url(settings.redis_connection_url, decode_responses=True)
    return EpisodeExtractor(
        settings, init_database(settings), GraphitiStore(settings), GraphVersions(redis_client, settings.app_env),
        GraphGuard(EXTRACTION_BREAKER, settings.graph_breaker_failures, settings.graph_breaker_reset_s))
