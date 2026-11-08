"""Allow-list of live refresh tokens in Redis (key = jti, value = user id).

A refresh token is valid only while its jti is listed: refresh consumes it (single use, so a replayed token is
rejected), logout revokes it, and Redis expires it with the token.
"""
from typing import Optional

from redis.asyncio import Redis

from agent_common.redis_keys import Keys, build_key


class RefreshTokenStore:
    def __init__(self, redis: Redis, env: str):
        self._redis, self._env = redis, env

    def _key(self, jti: str) -> str:
        return build_key(Keys.REFRESH_TOKEN, self._env, jti=jti)

    async def remember(self, jti: str, user_id: int, ttl_s: int) -> None:
        """List a newly issued refresh token for its lifetime."""
        await self._redis.setex(self._key(jti), ttl_s, str(user_id))

    async def consume(self, jti: str) -> Optional[int]:
        """Remove the token from the list and return its user id (None when it was not listed: used or revoked)."""
        owner = await self._redis.getdel(self._key(jti))
        return int(owner) if owner and str(owner).isdigit() else None

    async def revoke(self, jti: str) -> None:
        """Remove the token from the list (logout). Idempotent."""
        await self._redis.delete(self._key(jti))
