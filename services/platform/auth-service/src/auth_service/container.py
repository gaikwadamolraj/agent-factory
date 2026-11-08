"""Process-wide runtime objects of the auth-service, created once at startup and kept on `app.state.container`.

Tests build a Container themselves (fake Redis, in-memory keys) and set it before the first request.
"""
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Request

from agent_common.authz import AuthzService, SnapshotLoader, database_snapshot_loader
from agent_common.db import Database, init_database
from agent_common.redis_keys import Keys
from auth_service.clients import ServiceClients
from auth_service.config import AuthSettings
from auth_service.invites import InviteService
from auth_service.passwords import PasswordHasher, build_password_hasher
from auth_service.refresh_store import RefreshTokenStore
from auth_service.throttle import FailureThrottle
from auth_service.tokens import TokenIssuer


@dataclass
class Container:
    settings: AuthSettings
    redis: aioredis.Redis
    tokens: TokenIssuer
    clients: ServiceClients
    login_throttle: FailureThrottle
    client_throttle: FailureThrottle
    refresh_tokens: RefreshTokenStore
    authz: AuthzService
    passwords: PasswordHasher
    invites: InviteService
    db: Optional[Database] = None


def build_container(settings: AuthSettings, redis: aioredis.Redis, tokens: TokenIssuer, load_snapshot: SnapshotLoader,
                    db: Optional[Database] = None) -> Container:
    """Wire the Redis-backed helpers, the authz versions (snapshots from `load_snapshot`), the client registry, the
    password hasher (versioned pepper) and the invite service."""
    env = settings.app_env
    return Container(
        settings=settings, redis=redis, tokens=tokens, clients=ServiceClients.from_settings(settings),
        login_throttle=FailureThrottle(redis, env, Keys.LOGIN_ATTEMPTS, settings.login_max_failures),
        client_throttle=FailureThrottle(redis, env, Keys.CLIENT_AUTH_ATTEMPTS, settings.client_auth_max_failures),
        refresh_tokens=RefreshTokenStore(redis, env),
        authz=AuthzService(redis, env, load_snapshot, settings.authz_version_cache_ttl_s),
        passwords=build_password_hasher(settings), invites=InviteService.from_settings(settings), db=db)


async def start_container(settings: AuthSettings, tokens: TokenIssuer) -> Container:
    """Production startup: connect Redis (fails fast when unreachable) and the database pool."""
    redis_client = aioredis.from_url(settings.redis_connection_url, decode_responses=True)
    await redis_client.ping()
    db = init_database(settings)
    return build_container(settings, redis_client, tokens, database_snapshot_loader(db), db)


async def stop_container(container: Container) -> None:
    """Close Redis and the database pool."""
    await container.redis.aclose()
    if container.db is not None:
        await container.db.close()


def get_container(request: Request) -> Container:
    """FastAPI dependency: the container of this app."""
    return request.app.state.container
