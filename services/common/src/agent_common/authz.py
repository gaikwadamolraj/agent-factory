"""Versioned authorization data: zero trust without calling the auth-service on every request.

    authz = AuthzService(redis, settings.app_env, database_snapshot_loader(db), version_cache_ttl_s=5)
    await authz.require_current(claims)                 # AUTH-EXP-0002 when the token predates a change
    snapshot = await authz.snapshot(claims.user_id, claims.client_id)   # client, role, active, project ids
    await bump_authz_version(redis, env, user_id)       # after changing a user's role / state / memberships

How it works
* Every user has a version counter in Redis (Keys.AUTHZ_USER_VERSION), every client one too
  (Keys.AUTHZ_CLIENT_VERSION). The user's *authz version* is their sum. Both only ever increase, so any change
  increases the sum, and one number identifies one state of the user's permissions.
* The auth-service writes the authz version into every access token (claim `authz_ver`). A token with an older
  version is rejected (AUTH-EXP-0002, retryable): the UI refreshes silently and gets a token with the new version.
* Role, active state and project memberships are cached per version (Keys.AUTHZ_SNAPSHOT "authz:user:{id}:v{n}").
  A bump changes the key, so a stale snapshot is never read; the database is read only on a cache miss.
* Reading the version costs one Redis round trip; it is cached in-process for `version_cache_ttl_s` (default 5 s),
  so a change reaches other replicas within that time. The process that bumps sees it at once.
* Version keys have no TTL, so Redis never evicts them (maxmemory-policy volatile-lru only evicts keys with a TTL).
"""
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional

from agent_common.db import Database
from agent_common.db.repositories import ClientRepository, ProjectRepository, UserRepository
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.redis_keys import Keys, build_key, ttl
from agent_common.security import UserClaims

log = get_logger(__name__)


@dataclass(frozen=True)
class AuthzSnapshot:
    """What a user may do at one authz version."""

    user_id: int
    client_id: int
    role: str
    active: bool                    # user active AND client active
    project_ids: frozenset[int]     # active projects the user is a member of

    def to_json(self) -> str:
        return json.dumps({"userId": self.user_id, "clientId": self.client_id, "role": self.role,
                           "active": self.active, "projectIds": sorted(self.project_ids)})

    @classmethod
    def from_json(cls, raw: str) -> "AuthzSnapshot":
        data = json.loads(raw)
        return cls(int(data["userId"]), int(data["clientId"]), str(data["role"]), bool(data["active"]),
                   frozenset(int(p) for p in data["projectIds"]))


# user id -> snapshot from the database (None when the user does not exist)
SnapshotLoader = Callable[[int], Awaitable[Optional[AuthzSnapshot]]]


def database_snapshot_loader(db: Database) -> SnapshotLoader:
    """SnapshotLoader reading users, clients, projects and memberships through the shared repositories."""
    async def load(user_id: int) -> Optional[AuthzSnapshot]:
        async with db.session() as session:
            user = await UserRepository(session).get(user_id)
            if user is None:
                return None
            client = await ClientRepository(session).get(user.client_id)
            projects = await ProjectRepository(session).list_for_user(user.id, user.client_id)
            return AuthzSnapshot(user.id, user.client_id, user.role,
                                 bool(user.is_active and client is not None and client.is_active),
                                 frozenset(p.id for p in projects))
    return load


async def bump_authz_version(redis, env: str, user_id: int) -> int:
    """Invalidate a user's tokens and cached permissions (role, active state or memberships changed).
    Returns the new user counter."""
    return int(await redis.incr(build_key(Keys.AUTHZ_USER_VERSION, env, user_id=user_id)))


async def bump_client_authz_version(redis, env: str, client_id: int) -> int:
    """Invalidate the tokens and cached permissions of every user of a client (client deactivated, projects
    changed). Returns the new client counter."""
    return int(await redis.incr(build_key(Keys.AUTHZ_CLIENT_VERSION, env, client_id=client_id)))


async def bump_policy_version(redis, env: str) -> int:
    """Invalidate every subject's compiled-policy cache (an access policy, group or group/project membership
    changed). One global counter is enough: it is part of every compiled-policy cache key (agent_common.policy),
    so any bump makes all those keys miss and the next read recompiles from the database. Returns the new value."""
    return int(await redis.incr(build_key(Keys.POLICY_VERSION, env)))


async def read_policy_version(redis, env: str) -> int:
    """Current global policy version (0 when nothing has bumped it yet)."""
    return int(await redis.get(build_key(Keys.POLICY_VERSION, env)) or 0)


async def bump_skill_version(redis, env: str) -> int:
    """Invalidate every replica's skill-resolution cache (a skill's active/default published version changed:
    activate, archive or rollback). One global counter is part of every skill-resolution cache key, so a bump makes
    those keys miss and the new default is picked up at once. Returns the new value."""
    return int(await redis.incr(build_key(Keys.SKILL_ACTIVE_VERSION, env)))


async def read_skill_version(redis, env: str) -> int:
    """Current global skill-resolution version (0 when nothing has bumped it yet)."""
    return int(await redis.get(build_key(Keys.SKILL_ACTIVE_VERSION, env)) or 0)


class AuthzService:
    """Current authz versions (short in-process cache) and per-version permission snapshots (Redis, then DB)."""

    def __init__(self, redis, env: str, load_snapshot: SnapshotLoader, version_cache_ttl_s: float = 5.0,
                 max_cached_versions: int = 10_000, clock: Callable[[], float] = time.monotonic):
        self._redis, self._env, self._load_snapshot = redis, env, load_snapshot
        self._version_cache_ttl_s, self._max_cached, self._clock = version_cache_ttl_s, max_cached_versions, clock
        self._versions: OrderedDict[tuple[int, int], tuple[int, float]] = OrderedDict()   # bounded LRU

    async def current_version(self, user_id: int, client_id: int, fresh: bool = False) -> int:
        """The user's authz version (user counter + client counter). `fresh` skips the in-process cache
        (the auth-service uses it when it issues a token)."""
        cache_key = (user_id, client_id)
        cached = self._versions.get(cache_key)
        if cached and not fresh and self._clock() < cached[1]:
            return cached[0]
        user_counter, client_counter = await self._redis.mget(
            build_key(Keys.AUTHZ_USER_VERSION, self._env, user_id=user_id),
            build_key(Keys.AUTHZ_CLIENT_VERSION, self._env, client_id=client_id))
        version = int(user_counter or 0) + int(client_counter or 0)
        self._remember_version(cache_key, version)
        return version

    async def require_current(self, claims: UserClaims) -> None:
        """Raise AppError(AUTH-EXP-0002) when the token was issued before the user's last permission change."""
        current = await self.current_version(claims.user_id, claims.client_id)
        if claims.authz_ver < current:
            log_event(log, "authz.stale_token", token_version=claims.authz_ver, current_version=current)
            raise AppError(Errors.AUTH_PERMISSIONS_CHANGED)

    async def snapshot(self, user_id: int, client_id: int) -> Optional[AuthzSnapshot]:
        """Permissions of the user at the current version: Redis snapshot, or the database on a miss (then cached).
        None when the user does not exist."""
        version = await self.current_version(user_id, client_id)
        key = build_key(Keys.AUTHZ_SNAPSHOT, self._env, user_id=user_id, version=version)
        raw = await self._redis.get(key)
        if raw is not None:
            return AuthzSnapshot.from_json(raw)
        snapshot = await self._load_snapshot(user_id)
        if snapshot is not None:
            await self._redis.setex(key, ttl(Keys.AUTHZ_SNAPSHOT), snapshot.to_json())
        return snapshot

    async def bump_user(self, user_id: int) -> None:
        """bump_authz_version + forget this process's cached version, so the change applies here at once."""
        await bump_authz_version(self._redis, self._env, user_id)
        for cache_key in [k for k in self._versions if k[0] == user_id]:
            del self._versions[cache_key]

    async def bump_client(self, client_id: int) -> None:
        """bump_client_authz_version + forget this process's cached versions of that client."""
        await bump_client_authz_version(self._redis, self._env, client_id)
        for cache_key in [k for k in self._versions if k[1] == client_id]:
            del self._versions[cache_key]

    def _remember_version(self, cache_key: tuple[int, int], version: int) -> None:
        self._versions[cache_key] = (version, self._clock() + self._version_cache_ttl_s)
        self._versions.move_to_end(cache_key)
        while len(self._versions) > self._max_cached:
            self._versions.popitem(last=False)
