"""Brute-force protection: count failures per subject (username, client id) in Redis and lock after N.

The counter lives for the key's TTL (15 minutes) and restarts with every failure; a success clears it. Subjects are
hashed (`redis_keys.digest`), so usernames never appear in Redis keys.
"""
from typing import Optional

from redis.asyncio import Redis

from agent_common.redis_keys import Keys, build_key, digest, ttl


class FailureThrottle:
    """Failure counter for one kind of credential (Keys.LOGIN_ATTEMPTS or Keys.CLIENT_AUTH_ATTEMPTS)."""

    def __init__(self, redis: Redis, env: str, key: Keys, max_failures: int):
        self._redis, self._env, self._key, self._max_failures = redis, env, key, max_failures

    def _key_for(self, subject: str) -> str:
        return build_key(self._key, self._env, subject=digest(subject.lower()))

    async def retry_after(self, subject: str) -> Optional[int]:
        """Seconds until `subject` may try again, or None when it is not locked."""
        key = self._key_for(subject)
        failures = int(await self._redis.get(key) or 0)
        if failures < self._max_failures:
            return None
        return max(1, int(await self._redis.ttl(key)))

    async def record_failure(self, subject: str) -> None:
        """Count one failed attempt (the 15-minute window restarts)."""
        key = self._key_for(subject)
        await self._redis.incr(key)
        await self._redis.expire(key, ttl(self._key))

    async def reset(self, subject: str) -> None:
        """Forget the failures after a successful attempt."""
        await self._redis.delete(self._key_for(subject))
