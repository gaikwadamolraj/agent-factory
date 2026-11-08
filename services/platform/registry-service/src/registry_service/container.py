"""Process-wide runtime objects of registry-service, created once at startup and shared by every request.

Wires the shared agent_common building blocks: the token verifier (JWKS), the IAM policy engine (tenant-scoped
loader + versioned Redis cache), the authz-version service, the out-of-app notifier, the service-token provider, the
delegated-identity client and the tool catalogue. Tests build a Container with injected fakes.
"""
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis

from agent_common.authz import AuthzService, database_snapshot_loader
from agent_common.db import AccessPolicyRepository, Database, init_database
from agent_common.notifications import LogNotifier, Notifier, NullNotifier
from agent_common.policy import CachedPolicyLoader, InCodePolicyEngine, PolicyEngine, Subject
from agent_common.security import JwksVerifier, ServiceTokenProvider, build_jwks_verifier, build_service_token_provider

from registry_service.catalogue import ToolCatalogue
from registry_service.config import FactorySettings
from registry_service.identity_client import IdentityClient


def tenant_scoped_policy_loader(db: Database):
    """A policy loader that reads a subject's effective policies under the subject's own tenant context, so RLS on
    `access_policies` passes (the plain shared loader does not set the GUC)."""
    async def load(subject: Subject):
        async with db.tenant_session(subject.client_id) as session:
            return await AccessPolicyRepository(session).effective_for(subject)
    return load


@dataclass
class Container:
    settings: FactorySettings
    env: str
    redis: aioredis.Redis
    db: Database
    verifier: JwksVerifier
    policy_engine: PolicyEngine
    authz: AuthzService
    notifier: Notifier
    catalogue: ToolCatalogue
    tool_policy: dict[str, set[str]]
    tokens: Optional[ServiceTokenProvider] = None
    identity: Optional[IdentityClient] = None


def build_container(settings: FactorySettings, *, redis: aioredis.Redis, db: Database,
                    verifier: Optional[JwksVerifier] = None,
                    tokens: Optional[ServiceTokenProvider] = None) -> Container:
    """Assemble the container from its already-connected dependencies (so tests can inject fakes)."""
    env = settings.app_env
    verifier = verifier or build_jwks_verifier(settings)
    loader = CachedPolicyLoader(redis, env, tenant_scoped_policy_loader(db),
                                version_cache_ttl_s=settings.authz_version_cache_ttl_s)
    policy_engine = InCodePolicyEngine(loader)
    authz = AuthzService(redis, env, database_snapshot_loader(db), settings.authz_version_cache_ttl_s)
    notifier: Notifier = NullNotifier() if settings.is_prod else LogNotifier()
    catalogue = ToolCatalogue(settings.mcp_registry_map(), tokens)
    identity = (IdentityClient(settings.auth_service_base_url, settings.auth_service_audience, tokens)
                if tokens is not None else None)
    return Container(settings=settings, env=env, redis=redis, db=db, verifier=verifier, policy_engine=policy_engine,
                     authz=authz, notifier=notifier, catalogue=catalogue, tool_policy=settings.tool_policy_map(),
                     tokens=tokens, identity=identity)


async def start_container(settings: FactorySettings) -> Container:
    """Production startup: connect Redis (fails fast when unreachable) and the database pool, build the service
    token provider when a client secret is configured (needed for identity + catalogue)."""
    redis_client = aioredis.from_url(settings.redis_connection_url, decode_responses=True)
    await redis_client.ping()
    db = init_database(settings)
    tokens = None
    if settings.service_client_secret is not None:
        tokens = build_service_token_provider(settings)
    return build_container(settings, redis=redis_client, db=db, tokens=tokens)


async def stop_container(container: Container) -> None:
    """Close Redis and the database pool."""
    await container.redis.aclose()
    await container.db.close()


# The active container, set at startup. Both the REST handlers and the MCP tool wrappers reach it from here, so they
# do not depend on which Starlette app object holds request state.
_ACTIVE: Optional[Container] = None


def set_active_container(container: Optional[Container]) -> None:
    """Record (or clear) the process's active container."""
    global _ACTIVE
    _ACTIVE = container


def active_container() -> Container:
    """The active container; raises when the service has not finished starting."""
    if _ACTIVE is None:
        raise RuntimeError("registry-service container is not initialised yet.")
    return _ACTIVE
