"""Token verification (JwksVerifier), service tokens (ServiceTokenProvider) and the auth middlewares, with an
in-memory signing key (LocalTokenIssuer) instead of a running auth-service."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_common.constants import COOKIE_ACCESS_TOKEN, JWT_AUDIENCE_SKILL
from agent_common.errors import AppError, Errors
from agent_common.fastapi_errors import install_error_handlers
from agent_common.middleware import JWTAuthMiddleware, PathRules, ServiceAuthMiddleware
from agent_common.security import (HttpTokenFetcher, JwksVerifier, ServiceTokenProvider, TokenRequestRefused,
                                 user_claims_from, verify_service_token, verify_user_token)
from agent_common.security.keys import jwk_thumbprint
from agent_common.security.testing import LocalTokenIssuer


class FakeClock:
    """Monotonic clock the test moves forward by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------------------------------------ keys
def test_kid_is_the_rfc7638_thumbprint_and_differs_per_key():
    first, second = LocalTokenIssuer(), LocalTokenIssuer()
    assert first.kid == jwk_thumbprint(first.jwk) and len(first.kid) == 43      # base64url of 32 bytes
    assert first.kid != second.kid
    assert first.jwk["alg"] == "ES256" and first.jwk["use"] == "sig" and "d" not in first.jwk   # public only


# ------------------------------------------------------------------------------------------------ JwksVerifier
async def test_valid_user_token_is_verified_and_keys_are_fetched_once():
    issuer = LocalTokenIssuer()
    verifier = issuer.verifier()
    for _ in range(3):
        claims = await verify_user_token(verifier, issuer.user_token(user_id=7, client_id=2, role="admin",
                                                                     username="acme.admin"))
    assert (claims.user_id, claims.client_id, claims.role, claims.username) == (7, 2, "admin", "acme.admin")
    assert issuer.jwks_fetches == 1


async def test_expired_token_is_rejected_as_expired():
    issuer = LocalTokenIssuer()
    with pytest.raises(AppError) as error:
        await verify_user_token(issuer.verifier(leeway_s=0), issuer.user_token(ttl_s=-60))
    assert error.value.spec is Errors.AUTH_TOKEN_EXPIRED


async def test_small_clock_skew_is_tolerated_by_the_leeway():
    issuer = LocalTokenIssuer()
    claims = await verify_user_token(issuer.verifier(leeway_s=30), issuer.user_token(ttl_s=-5))
    assert claims.user_id == 1


@pytest.mark.parametrize("make_token", [
    lambda issuer: issuer.service_token("ats-orchestrator", "agent-skill"),      # wrong audience
    lambda issuer: LocalTokenIssuer(issuer="http://evil").user_token(),                  # other key + issuer
    lambda issuer: "not-a-jwt",
])
async def test_wrong_audience_foreign_key_or_garbage_is_invalid(make_token):
    issuer = LocalTokenIssuer()
    with pytest.raises(AppError) as error:
        await verify_user_token(issuer.verifier(), make_token(issuer))
    assert error.value.spec is Errors.AUTH_TOKEN_INVALID


async def test_unknown_kid_refetches_once_and_accepts_the_rotated_key():
    old, new = LocalTokenIssuer(), LocalTokenIssuer()
    published = {"keys": [old.jwk]}
    fetches = []

    async def fetch():
        fetches.append(1)
        return published

    clock = FakeClock()
    verifier = JwksVerifier(fetch, old.issuer, min_refetch_interval_s=10, clock=clock)
    assert (await verifier.verify(old.user_token(), "ats-api"))["sub"] == "1"

    published = {"keys": [new.jwk, old.jwk]}            # rotation: new key signs, old key still published
    clock.now += 11
    assert (await verifier.verify(new.user_token(user_id=9), "ats-api"))["sub"] == "9"
    assert (await verifier.verify(old.user_token(user_id=4), "ats-api"))["sub"] == "4"   # old tokens still valid
    assert len(fetches) == 2


async def test_unknown_kids_cannot_force_more_than_one_refetch_per_interval():
    issuer, stranger = LocalTokenIssuer(), LocalTokenIssuer()       # same issuer name, unpublished key
    clock = FakeClock()
    verifier = issuer.verifier(min_refetch_interval_s=10, clock=clock)
    await verifier.verify(issuer.user_token(), "ats-api")
    clock.now += 11
    for _ in range(5):                                 # flood of tokens with a kid nobody published
        with pytest.raises(AppError) as error:
            await verifier.verify(stranger.user_token(), "ats-api")
        assert error.value.spec is Errors.AUTH_TOKEN_INVALID
    assert issuer.jwks_fetches == 2                    # first load + one refetch, not six


async def test_cached_keys_keep_working_when_the_auth_service_is_down():
    issuer = LocalTokenIssuer()
    clock = FakeClock()
    available = True

    async def fetch():
        if not available:
            raise ConnectionError("auth-service unreachable")
        return await issuer.fetch_jwks()

    verifier = JwksVerifier(fetch, issuer.issuer, cache_ttl_s=60, clock=clock)
    await verifier.verify(issuer.user_token(), "ats-api")
    available = False
    clock.now += 120                                  # cache is stale, refetch fails -> stale keys still used
    assert (await verifier.verify(issuer.user_token(user_id=3), "ats-api"))["sub"] == "3"


async def test_without_any_key_an_unreachable_auth_service_is_reported_as_unavailable():
    issuer = LocalTokenIssuer()

    async def down():
        raise ConnectionError("auth-service unreachable")

    verifier = JwksVerifier(down, issuer.issuer)
    with pytest.raises(AppError) as error:
        await verifier.verify(issuer.user_token(), "ats-api")
    assert error.value.spec is Errors.UPSTREAM_UNAVAILABLE and error.value.retry_after


async def test_service_token_gives_the_caller_and_user_tokens_are_not_service_tokens():
    issuer = LocalTokenIssuer()
    verifier = issuer.verifier()
    caller = await verify_service_token(verifier, issuer.service_token("ats-orchestrator", "agent-skill"),
                                        JWT_AUDIENCE_SKILL)
    assert caller == "ats-orchestrator"
    forged = issuer.sign({"aud": JWT_AUDIENCE_SKILL, "sub": "1", "cid": 1})          # no kind=service
    with pytest.raises(AppError):
        await verify_service_token(verifier, forged, JWT_AUDIENCE_SKILL)


def test_service_claims_are_never_user_claims():
    with pytest.raises(AppError):
        user_claims_from({"sub": "ats-orchestrator", "cid": 1, "kind": "service"})


# ------------------------------------------------------------------------------------------------ service tokens
async def test_service_tokens_are_cached_per_audience_until_80_percent_of_their_lifetime():
    calls = []
    clock = FakeClock()

    async def fetch(audience):
        calls.append(audience)
        return f"token-{audience}-{len(calls)}", 300

    provider = ServiceTokenProvider(fetch, refresh_ratio=0.8, clock=clock)
    assert await provider.token("agent-skill") == "token-agent-skill-1"
    assert await provider.token("agent-skill") == "token-agent-skill-1"            # cached
    assert await provider.token("agent-compute") == "token-agent-compute-2"        # other audience, own token
    clock.now += 239
    assert await provider.token("agent-skill") == "token-agent-skill-1"            # 79.7% of 300 s
    clock.now += 2
    assert await provider.token("agent-skill") == "token-agent-skill-3"            # renewed after 80%
    assert calls == ["agent-skill", "agent-compute", "agent-skill"]


async def test_refused_token_request_becomes_service_forbidden(monkeypatch):
    fetcher = HttpTokenFetcher("http://auth.test/oauth/token", "ats-orchestrator", "s" * 40, retry_attempts=1)

    async def refused(audience):
        raise TokenRequestRefused(403)

    monkeypatch.setattr(fetcher, "_post", refused)
    with pytest.raises(AppError) as error:
        await fetcher("agent-skill")
    assert error.value.spec is Errors.SERVICE_FORBIDDEN


async def test_unreachable_token_endpoint_becomes_unavailable_without_internal_text(monkeypatch):
    fetcher = HttpTokenFetcher("http://auth.test/oauth/token", "ats-orchestrator", "s" * 40, retry_attempts=1)

    async def unreachable(audience):
        raise ConnectionError("10.0.0.9 refused")

    monkeypatch.setattr(fetcher, "_post", unreachable)
    with pytest.raises(AppError) as error:
        await fetcher("agent-skill")
    assert error.value.spec is Errors.UPSTREAM_UNAVAILABLE and "10.0.0.9" not in str(error.value)


# ------------------------------------------------------------------------------------------------ middlewares
def _user_app(issuer: LocalTokenIssuer) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.api_route("/api/v1/me", methods=["GET", "POST"])
    async def me(request: Request):
        return {"userId": request.state.user.user_id}

    app.add_middleware(JWTAuthMiddleware, verifier=issuer.verifier(), rules=PathRules(protected=("/api/v1",)))
    return TestClient(app)


def test_user_middleware_accepts_bearer_and_cookie_and_guards_cookie_writes():
    issuer = LocalTokenIssuer()
    client = _user_app(issuer)
    token = issuer.user_token(user_id=5)
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).json() == {"userId": 5}
    client.cookies.set(COOKIE_ACCESS_TOKEN, token)
    assert client.get("/api/v1/me").json() == {"userId": 5}
    assert client.post("/api/v1/me").json()["error"]["code"] == "AUTH-FBD-0001"           # CSRF header missing
    assert client.post("/api/v1/me", headers={"X-Requested-With": "test"}).status_code == 200


def test_user_middleware_rejects_missing_expired_and_service_tokens():
    issuer = LocalTokenIssuer()
    client = _user_app(issuer)
    assert client.get("/api/v1/me").json()["error"]["code"] == "AUTH-MIS-0001"
    expired = client.get("/api/v1/me", headers={"Authorization": f"Bearer {issuer.user_token(ttl_s=-120)}"})
    assert expired.status_code == 401 and expired.json()["error"]["code"] == "AUTH-EXP-0001"
    service = issuer.service_token("ats-orchestrator", "ats-api")
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {service}"}).status_code == 401


def test_service_middleware_checks_audience_and_allowed_callers():
    issuer = LocalTokenIssuer()

    async def mcp(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": scope["state"]["caller_service"].encode()})

    app = ServiceAuthMiddleware(mcp, issuer.verifier(), "agent-skill", PathRules(protected=("/mcp",)),
                                {"ats-orchestrator"})
    client = TestClient(app)

    def call(token):
        return client.post("/mcp", headers={"Authorization": f"Bearer {token}"})

    ok = call(issuer.service_token("ats-orchestrator", "agent-skill"))
    assert ok.status_code == 200 and ok.text == "ats-orchestrator"
    assert call(issuer.service_token("ats-orchestrator", "agent-compute")).status_code == 401  # other audience
    assert call(issuer.service_token("intruder", "agent-skill")).status_code == 403                 # not allowed
    assert call(issuer.user_token()).status_code == 401                                             # user token
    assert client.post("/mcp").status_code == 401
