"""Versioned authorization (agent_common.authz) and the tenant middleware built on it: stale tokens rejected after a
bump, snapshot cache hits avoid the database, a bump makes the next read use the new version."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_common.authz import AuthzService, AuthzSnapshot, bump_authz_version
from agent_common.config import CommonSettings
from agent_common.errors import AppError, Errors
from agent_common.fastapi_errors import install_error_handlers
from agent_common.middleware import JWTAuthMiddleware, PathRules, TenantMiddleware
from agent_common.security import UserClaims
from agent_common.security.testing import LocalTokenIssuer
from agent_common.testing import FakeRedis


class CountingLoader:
    """SnapshotLoader stand-in: the 'database' of users, counting how often it is read."""

    def __init__(self):
        self.reads = 0
        self.users = {7: AuthzSnapshot(7, 1, "recruiter", True, frozenset({10}))}

    async def __call__(self, user_id):
        self.reads += 1
        return self.users.get(user_id)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def claims(authz_ver: int) -> UserClaims:
    return UserClaims(user_id=7, client_id=1, role="recruiter", username="u", authz_ver=authz_ver)


async def test_token_with_an_older_authz_version_is_rejected_after_a_bump():
    redis, loader = FakeRedis(), CountingLoader()
    authz = AuthzService(redis, "dev", loader)
    await authz.require_current(claims(0))                       # nothing changed yet: version 0 is current
    await authz.bump_user(7)                                      # e.g. an admin removed a project membership
    with pytest.raises(AppError) as error:
        await authz.require_current(claims(0))
    assert error.value.spec is Errors.AUTH_PERMISSIONS_CHANGED and error.value.spec.retryable
    await authz.require_current(claims(1))                       # the refreshed token carries the new version


async def test_client_wide_bump_also_invalidates_the_users_tokens():
    redis = FakeRedis()
    authz = AuthzService(redis, "dev", CountingLoader())
    await authz.bump_client(1)
    with pytest.raises(AppError):
        await authz.require_current(claims(0))
    assert await authz.current_version(7, 1) == 1


async def test_snapshot_cache_hit_avoids_the_database():
    redis, loader = FakeRedis(), CountingLoader()
    authz = AuthzService(redis, "dev", loader)
    first = await authz.snapshot(7, 1)
    second = await authz.snapshot(7, 1)
    assert first == second and first.project_ids == frozenset({10})
    assert loader.reads == 1                                      # second read came from Redis


async def test_after_a_bump_the_new_version_is_loaded_and_used():
    redis, loader = FakeRedis(), CountingLoader()
    authz = AuthzService(redis, "dev", loader)
    assert 11 not in (await authz.snapshot(7, 1)).project_ids
    loader.users[7] = AuthzSnapshot(7, 1, "recruiter", True, frozenset({10, 11}))   # membership added in the DB
    await authz.bump_user(7)
    assert (await authz.snapshot(7, 1)).project_ids == frozenset({10, 11})
    assert loader.reads == 2
    assert any(key.endswith(":v1") for key in redis.values)          # snapshot key carries the version


async def test_version_is_cached_in_process_for_the_configured_time():
    redis, clock = FakeRedis(), FakeClock()
    authz = AuthzService(redis, "dev", CountingLoader(), version_cache_ttl_s=5, clock=clock)
    await authz.current_version(7, 1)
    await authz.current_version(7, 1)
    assert redis.calls.count("mget") == 1
    await bump_authz_version(redis, "dev", 7)                     # bumped by another replica
    assert await authz.current_version(7, 1) == 0                 # still cached here ...
    clock.now += 6
    assert await authz.current_version(7, 1) == 1                 # ... and seen once the cache entry expired


def test_redis_db_override_replaces_the_database_number():
    settings = CommonSettings(service_name="t", redis_url="redis://:pw@redis-service:6379/1", redis_db=0)
    assert settings.redis_connection_url == "redis://:pw@redis-service:6379/0"
    assert CommonSettings(service_name="t", redis_url="redis://h:6379/1").redis_connection_url == "redis://h:6379/1"


# ------------------------------------------------------------------------------------------------ middlewares
def _tenant_app(issuer: LocalTokenIssuer, authz: AuthzService) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/v1/chats")
    async def chats(request: Request):
        return {"tenant": list(request.state.tenant)}

    app.add_middleware(TenantMiddleware, rules=PathRules(protected=("/api/v1/chats",)), authz=lambda: authz)
    app.add_middleware(JWTAuthMiddleware, verifier=issuer.verifier(), rules=PathRules(protected=("/api/v1",)),
                       authz=lambda: authz)
    return TestClient(app)


def test_tenant_headers_must_match_the_token_client_and_a_membership():
    issuer, loader = LocalTokenIssuer(), CountingLoader()
    client = _tenant_app(issuer, AuthzService(FakeRedis(), "dev", loader))
    auth = {"Authorization": f"Bearer {issuer.user_token(user_id=7, client_id=1)}"}

    def get(client_id, project_id):
        return client.get("/api/v1/chats", headers={**auth, "X-Client-Id": str(client_id),
                                                     "X-Project-Id": str(project_id)})

    assert get(1, 10).json() == {"tenant": [1, 10]}
    assert get(2, 10).json()["error"]["code"] == "TEN-FBD-0001"          # other client than the token's
    assert get(1, 11).json()["error"]["code"] == "TEN-FBD-0001"          # not a member of project 11
    assert client.get("/api/v1/chats", headers=auth).json()["error"]["code"] == "TEN-MIS-0001"
    assert loader.reads == 1                                             # one DB read, the rest from the cache


def test_disabled_user_and_stale_token_are_refused():
    issuer, loader, redis = LocalTokenIssuer(), CountingLoader(), FakeRedis()
    authz = AuthzService(redis, "dev", loader)
    client = _tenant_app(issuer, authz)
    headers = {"Authorization": f"Bearer {issuer.user_token(user_id=7, client_id=1)}",
               "X-Client-Id": "1", "X-Project-Id": "10"}
    loader.users[7] = AuthzSnapshot(7, 1, "recruiter", False, frozenset({10}))   # disabled ...
    redis.values["ats:dev:authz:user:7:version"] = "1"                           # ... and bumped
    stale = client.get("/api/v1/chats", headers=headers)
    assert stale.status_code == 401 and stale.json()["error"]["code"] == "AUTH-EXP-0002"
    fresh = {**headers, "Authorization": f"Bearer {issuer.user_token(user_id=7, client_id=1, authz_ver=1)}"}
    assert client.get("/api/v1/chats", headers=fresh).json()["error"]["code"] == "TEN-FBD-0001"
