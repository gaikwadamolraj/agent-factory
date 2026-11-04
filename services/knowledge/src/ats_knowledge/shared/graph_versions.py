"""Per-graph change counter in Redis.

The worker bumps it when an episode lands in a graph and the API bumps it when a source is forgotten. The API's
semantic cache includes it in every cache scope, so a changed graph never serves an old answer.
"""
import redis.asyncio as aioredis
from ats_common.redis_keys import Keys, build_key


class GraphVersions:
    def __init__(self, redis: aioredis.Redis, env: str):
        self.redis, self.env = redis, env

    def _key(self, graph_id: str) -> str:
        return build_key(Keys.KNOWLEDGE_GRAPH_VERSION, self.env, graph_id=graph_id)

    async def current(self, graph_id: str) -> int:
        """Current version (0 for a graph that never changed)."""
        return int(await self.redis.get(self._key(graph_id)) or 0)

    async def bump(self, graph_id: str) -> int:
        """Mark the graph as changed; returns the new version."""
        return int(await self.redis.incr(self._key(graph_id)))
