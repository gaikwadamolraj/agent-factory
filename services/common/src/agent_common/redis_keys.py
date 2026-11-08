"""Single place that defines every Redis key: pattern, TTL, purpose and owner.

    key = build_key(Keys.SCREENING_RESULT, client_id=1, project_id=2, digest=...)
    await redis.setex(key, ttl(Keys.SCREENING_RESULT), value)

* Keys are namespaced by environment and tenant so data never leaks across clients.
* Parts are validated (no ':' injection, bounded length); long values are hashed.
* Cached configuration includes the config version (see ConfigVersion) so a config change refreshes caches.
"""
import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

_SAFE_PART = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
PREFIX = "ats"


@dataclass(frozen=True)
class KeySpec:
    template: str               # e.g. "tenant:{client_id}:{project_id}:member:{user_id}"
    ttl_seconds: Optional[int]  # None = no expiry (only for counters/versions)
    description: str
    used_by: str


class Keys(StrEnum):
    AUTHZ_USER_VERSION = "authz_user_version"
    AUTHZ_CLIENT_VERSION = "authz_client_version"
    AUTHZ_SNAPSHOT = "authz_snapshot"
    SCREENING_RESULT = "screening_result"
    TURN_ABORT = "turn_abort"
    AGENT_STEP_CACHE = "agent_step_cache"
    AGENT_MEMORY_SESSION = "agent_memory_session"
    AGENT_MEMORY_STATE = "agent_memory_state"
    AGENT_MEMORY_MESSAGES = "agent_memory_messages"
    CONFIG_VERSION = "config_version"
    LOGIN_ATTEMPTS = "login_attempts"
    CLIENT_AUTH_ATTEMPTS = "client_auth_attempts"
    REFRESH_TOKEN = "refresh_token"
    KNOWLEDGE_GRAPH_VERSION = "knowledge_graph_version"
    KNOWLEDGE_SEMANTIC_CACHE = "knowledge_semantic_cache"
    POLICY_VERSION = "policy_version"
    POLICY_COMPILED = "policy_compiled"
    SKILL_ACTIVE_VERSION = "skill_active_version"
    FACTORY_RATE_LIMIT = "factory_rate_limit"


REGISTRY: dict[Keys, KeySpec] = {
    Keys.AUTHZ_USER_VERSION: KeySpec("authz:user:{user_id}:version", None,
                                     "Counter bumped when a user's role, active state or project memberships change; "
                                     "part of the authz version in access tokens and of the snapshot key "
                                     "(no TTL: never evicted under volatile-lru)", "auth-service"),
    Keys.AUTHZ_CLIENT_VERSION: KeySpec("authz:client:{client_id}:version", None,
                                       "Counter bumped for client-wide changes (client deactivated, projects of "
                                       "the client changed); added to every user's authz version", "auth-service"),
    Keys.AUTHZ_SNAPSHOT: KeySpec("authz:user:{user_id}:v{version}", 3600,
                                 "Cached {clientId, role, active, projectIds} of a user at one authz version "
                                 "(DB only on a miss; a bump makes a new key)", "agent_common.authz"),
    Keys.SCREENING_RESULT: KeySpec("tenant:{client_id}:{project_id}:screening:{digest}", 24 * 3600,
                                   "Screening result for an identical JD + resume + skill version", "orchestrator"),
    Keys.TURN_ABORT: KeySpec("turn:{turn_id}:abort", 3600,
                             "Abort flag for a running chat turn (checked by all replicas)", "orchestrator"),
    Keys.AGENT_STEP_CACHE: KeySpec("agent:step:{kind}:{digest}", 7 * 24 * 3600,
                                   "Cached JD/resume analysis per skill version + model", "agent"),
    Keys.AGENT_MEMORY_SESSION: KeySpec("agent:memory:{session_id}:session", 7 * 24 * 3600,
                                       "Strands session record (write-through cache of Postgres)", "agent"),
    Keys.AGENT_MEMORY_STATE: KeySpec("agent:memory:{session_id}:{agent_id}:state", 7 * 24 * 3600,
                                     "Strands agent state (workspace, history)", "agent"),
    Keys.AGENT_MEMORY_MESSAGES: KeySpec("agent:memory:{session_id}:{agent_id}:messages", 7 * 24 * 3600,
                                        "Strands conversation messages (hash: message_id -> json)", "agent"),
    Keys.CONFIG_VERSION: KeySpec("config:version", None,
                                 "Incremented on every runtime config change; part of cached config keys", "all"),
    Keys.LOGIN_ATTEMPTS: KeySpec("auth:login:{subject}", 900,
                                 "Failed sign-in counter per username digest (brute-force lock)", "auth-service"),
    Keys.CLIENT_AUTH_ATTEMPTS: KeySpec("auth:client:{subject}", 900,
                                       "Failed client_credentials counter per client id digest (brute-force lock)",
                                       "auth-service"),
    Keys.REFRESH_TOKEN: KeySpec("auth:refresh:{jti}", 7 * 24 * 3600,
                                "Allow-list of live refresh tokens (value = user id); deleted on rotation/logout",
                                "auth-service"),
    Keys.KNOWLEDGE_GRAPH_VERSION: KeySpec("knowledge:{graph_id}:version", None,
                                          "Counter bumped whenever a tenant graph changes; part of every semantic "
                                          "cache scope, so a changed graph never serves old answers", "knowledge"),
    Keys.KNOWLEDGE_SEMANTIC_CACHE: KeySpec("knowledge:semcache", 24 * 3600,
                                           "RedisVL index name + key prefix of the semantic answer cache (entries "
                                           "carry a tenant-scope tag and expire after the TTL)", "knowledge"),
    Keys.POLICY_VERSION: KeySpec("policy:version", None,
                                 "Counter bumped on any access-policy, group or membership change; part of every "
                                 "compiled-policy cache key, so no replica serves a stale policy "
                                 "(no TTL: never evicted under volatile-lru)", "registry-service"),
    Keys.POLICY_COMPILED: KeySpec("policy:{principal_type}:{principal_id}:v{version}", 300,
                                  "Compiled (effective) access policies of one subject at one policy version "
                                  "(DB only on a miss; a version bump makes a new key)", "registry-service"),
    Keys.SKILL_ACTIVE_VERSION: KeySpec("skill:version", None,
                                       "Counter bumped whenever a skill's active/default published version changes "
                                       "(activate/archive/rollback); part of every skill-resolution cache key so all "
                                       "replicas pick up a new default at once (no TTL: never evicted)", "registry-service"),
    Keys.FACTORY_RATE_LIMIT: KeySpec("factory:ratelimit:{principal}:{window}", 60,
                                     "Per-principal request counter in a fixed time window for the registry-service API "
                                     "rate-limit hook (expires with the window)", "registry-service"),
}

CONFIG_CHANGED_CHANNEL = f"{PREFIX}:config:changed"


def digest(*values: str) -> str:
    """Stable short hash for long/unsafe values (JD text, resume text, usernames)."""
    h = hashlib.sha256()
    for v in values:
        h.update(v.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def build_key(name: Keys, env: str = "dev", **parts: object) -> str:
    """Render a registered key. Raises ValueError for unknown names, missing or unsafe parts."""
    spec = REGISTRY[name]
    rendered: dict[str, str] = {}
    for field_name in re.findall(r"{(\w+)}", spec.template):
        if field_name not in parts:
            raise ValueError(f"Key {name} needs '{field_name}'")
        value = str(parts[field_name])
        if not _SAFE_PART.match(value):
            raise ValueError(f"Unsafe value for key part '{field_name}'")
        rendered[field_name] = value
    return f"{PREFIX}:{env.lower()}:" + spec.template.format(**rendered)


def ttl(name: Keys) -> Optional[int]:
    return REGISTRY[name].ttl_seconds


class ConfigVersion:
    """Local view of the global config version; bump() publishes a change so every replica refreshes caches."""

    def __init__(self, redis_client, env: str = "dev"):
        self.redis, self.env, self.value = redis_client, env, 0

    async def refresh(self) -> int:
        raw = await self.redis.get(build_key(Keys.CONFIG_VERSION, self.env))
        self.value = int(raw or 0)
        return self.value

    async def bump(self) -> int:
        self.value = int(await self.redis.incr(build_key(Keys.CONFIG_VERSION, self.env)))
        await self.redis.publish(CONFIG_CHANGED_CHANNEL, str(self.value))
        return self.value

    async def listen(self) -> None:
        """Run as a background task: updates `value` whenever any replica bumps the version."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(CONFIG_CHANGED_CHANNEL)
        async for message in pubsub.listen():
            if message.get("type") == "message":
                self.value = int(message["data"])
