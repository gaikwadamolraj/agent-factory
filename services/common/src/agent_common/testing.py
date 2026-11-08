"""Test helpers shared by the services' tests (never used by service code).

FakeRedis: the subset of redis.asyncio.Redis (decode_responses=True) the services use, in memory. TTLs are stored
but never expire.
"""


class FakeRedis:
    """In-memory stand-in for redis.asyncio.Redis; `calls` counts commands (for cache-hit assertions)."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[str] = []

    async def get(self, key):
        self.calls.append("get")
        return self.values.get(key)

    async def mget(self, *keys):
        self.calls.append("mget")
        return [self.values.get(key) for key in keys]

    async def setex(self, key, ttl_s, value):
        self.calls.append("setex")
        self.values[key], self.ttls[key] = str(value), int(ttl_s)

    async def incr(self, key):
        self.calls.append("incr")
        self.values[key] = str(int(self.values.get(key, 0)) + 1)
        return int(self.values[key])

    async def expire(self, key, ttl_s):
        self.calls.append("expire")
        self.ttls[key] = int(ttl_s)

    async def ttl(self, key):
        self.calls.append("ttl")
        return self.ttls.get(key, -1) if key in self.values else -2

    async def delete(self, key):
        self.calls.append("delete")
        self.ttls.pop(key, None)
        return 1 if self.values.pop(key, None) is not None else 0

    async def getdel(self, key):
        self.calls.append("getdel")
        self.ttls.pop(key, None)
        return self.values.pop(key, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass
