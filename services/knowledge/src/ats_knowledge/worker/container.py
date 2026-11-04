"""Wiring for the worker process (`ats-knowledge-worker`). Needs only the database, the graph store and the
graph-version counter - no semantic cache, no answer model."""
from functools import lru_cache

import redis.asyncio as aioredis
from ats_common.db import init_database

from ats_knowledge.config import get_settings
from ats_knowledge.shared.graph.graphiti_store import GraphitiStore
from ats_knowledge.shared.graph_guard import EXTRACTION_BREAKER, GraphGuard
from ats_knowledge.shared.graph_versions import GraphVersions
from ats_knowledge.worker.extraction import EpisodeExtractor


@lru_cache
def get_extractor() -> EpisodeExtractor:
    """Raises if a store is unreachable (e.g. FalkorDB still starting); not cached on failure, so callers retry."""
    settings = get_settings()
    if settings.redis_url is None:
        raise RuntimeError("REDIS_URL is required (graph versions).")
    redis_client = aioredis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    return EpisodeExtractor(
        settings, init_database(settings), GraphitiStore(settings), GraphVersions(redis_client, settings.app_env),
        GraphGuard(EXTRACTION_BREAKER, settings.graph_breaker_failures, settings.graph_breaker_reset_s))
