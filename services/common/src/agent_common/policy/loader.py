"""Loading a subject's *effective* policies, with a versioned Redis cache in front of the database.

The pure evaluator (agent_common.policy.evaluator) does not know where policies come from; this module supplies them.
A subject's effective policies are every enabled `access_policies` row attached to the subject itself and to each
group, project and client it belongs to.

Caching mirrors agent_common.authz / ConfigVersion: every compiled-policy cache key carries the global policy version
(Keys.POLICY_VERSION), bumped by `bump_policy_version` on any policy/group/membership change. A bump makes all those
keys miss, so no replica ever serves a stale policy. The version itself is read from Redis once and cached in-process
for a short TTL to avoid a round trip per request.
"""
import json
import time
from collections.abc import Awaitable, Callable

from agent_common.authz import read_policy_version
from agent_common.logging import get_logger, log_event
from agent_common.policy.models import PolicyDocument, Subject
from agent_common.redis_keys import Keys, build_key, ttl

log = get_logger(__name__)

# Loads a subject's effective policies straight from the source of truth (the database), no caching.
PolicyLoader = Callable[[Subject], Awaitable[list[PolicyDocument]]]


def database_policy_loader(db) -> PolicyLoader:
    """A PolicyLoader that reads effective policies through AccessPolicyRepository (one unit of work per load)."""
    # Imported here (not at module top) so agent_common.policy stays importable without a configured database.
    from agent_common.db.platform_repositories import AccessPolicyRepository

    async def load(subject: Subject) -> list[PolicyDocument]:
        async with db.session() as session:
            return await AccessPolicyRepository(session).effective_for(subject)
    return load


class CachedPolicyLoader:
    """Effective policies for a subject: compiled set from Redis (keyed by the policy version), DB on a miss.

    This is the thin, impure wrapper around the pure evaluator's inputs. It is safe to share across requests: the only
    mutable state is a short-lived in-process cache of the global policy version.
    """

    def __init__(self, redis, env: str, load_from_db: PolicyLoader, version_cache_ttl_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self._redis, self._env, self._load_from_db = redis, env, load_from_db
        self._version_cache_ttl_s, self._clock = version_cache_ttl_s, clock
        self._cached_version: tuple[int, float] | None = None   # (version, expires_at)

    async def current_version(self) -> int:
        """The global policy version, cached in-process for `version_cache_ttl_s` to save a round trip per request."""
        cached = self._cached_version
        if cached is not None and self._clock() < cached[1]:
            return cached[0]
        version = await read_policy_version(self._redis, self._env)
        self._cached_version = (version, self._clock() + self._version_cache_ttl_s)
        return version

    async def load(self, subject: Subject) -> list[PolicyDocument]:
        """Effective policies for the subject: the Redis compiled set at the current version, or the DB on a miss
        (then cached under that version's key)."""
        version = await self.current_version()
        key = build_key(Keys.POLICY_COMPILED, self._env, principal_type="user", principal_id=subject.user_id,
                        version=version)
        raw = await self._redis.get(key)
        if raw is not None:
            return [PolicyDocument.model_validate(doc) for doc in json.loads(raw)]
        # Cache miss: recompile from the database and store under this version's key.
        policies = await self._load_from_db(subject)
        await self._redis.setex(key, ttl(Keys.POLICY_COMPILED),
                                json.dumps([policy.model_dump(mode="json") for policy in policies]))
        log_event(log, "policy.compiled", user_id=subject.user_id, version=version, policies=len(policies))
        return policies
