"""client_credentials token endpoint, JWKS / discovery documents, signing keys and rotation."""
import pytest

from agent_common.constants import TOKEN_KIND_SERVICE
from agent_common.security import JwksVerifier, verify_service_token
from agent_common.security.keys import jwk_thumbprint
from auth_service.clients import ServiceClients
from auth_service.config import AuthSettings
from auth_service.signing import KeyRing, SigningKey
from auth_service.tokens import TokenIssuer
from conftest import ORCHESTRATOR_SECRET, TEST_ISSUER, pem_of_new_key


def grant(audience="agent-skill", secret=ORCHESTRATOR_SECRET, client_id="ats-orchestrator",
          grant_type="client_credentials"):
    return {"grant_type": grant_type, "client_id": client_id, "client_secret": secret, "audience": audience}


async def published_verifier(service) -> JwksVerifier:
    """A verifier fed with what /.well-known/jwks.json publishes (as another service would see it)."""
    jwks = (await service.client.get("/.well-known/jwks.json")).json()

    async def fetch():
        return jwks

    return JwksVerifier(fetch, TEST_ISSUER)


# ------------------------------------------------------------------------------------------------ /oauth/token
async def test_allowed_pair_gets_a_short_lived_service_token_for_that_audience(service):
    response = await service.client.post("/oauth/token", data=grant("agent-skill"))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 300
    verifier = await published_verifier(service)
    assert await verify_service_token(verifier, body["access_token"], "agent-skill") == "ats-orchestrator"
    claims = await verifier.verify(body["access_token"], "agent-skill")
    assert claims["kind"] == TOKEN_KIND_SERVICE and claims["exp"] - claims["iat"] == 300


async def test_pair_outside_the_policy_is_forbidden(service):
    response = await service.client.post("/oauth/token", data=grant("agent-knowledge-graph"))
    assert response.status_code == 403 and response.json()["error"]["code"] == "AUTH-FBD-0003"


async def test_user_audiences_can_never_be_requested(service):
    for audience in ("ats-api", "ats-refresh"):
        assert (await service.client.post("/oauth/token", data=grant(audience))).status_code == 403


async def test_wrong_secret_and_unknown_client_get_the_same_answer(service):
    for form in (grant(secret="x" * 48), grant(client_id="agent-unknown")):
        response = await service.client.post("/oauth/token", data=form)
        assert response.status_code == 401 and response.json()["error"]["code"] == "AUTH-INV-0003"


async def test_client_is_locked_after_max_failed_secrets(service):
    for _ in range(3):                                       # client_auth_max_failures=3 in the test settings
        await service.client.post("/oauth/token", data=grant(secret="x" * 48))
    locked = await service.client.post("/oauth/token", data=grant())     # even the right secret now waits
    assert locked.status_code == 429 and locked.json()["error"]["code"] == "AUTH-RTL-0002"
    assert int(locked.headers["Retry-After"]) > 0


async def test_other_grants_and_malformed_fields_are_rejected(service):
    unsupported = await service.client.post("/oauth/token", data=grant(grant_type="password"))
    assert unsupported.status_code == 400 and unsupported.json()["error"]["code"] == "AUTH-INV-0004"
    malformed = await service.client.post("/oauth/token", data=grant(audience="Agent Skill!"))
    assert malformed.status_code == 422 and malformed.json()["error"]["code"] == "VAL-INV-0001"


def test_client_without_a_secret_cannot_authenticate(monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_ATS_ORCHESTRATOR", "s" * 40)
    monkeypatch.delenv("CLIENT_SECRET_AGENT_KNOWLEDGE_GRAPH", raising=False)
    settings = AuthSettings(oauth_client_audiences="ats-orchestrator=agent-skill,"
                                                   "agent-knowledge-graph=agent-guardrail")
    clients = ServiceClients.from_settings(settings)
    assert clients.client_ids == ["ats-orchestrator"]
    assert clients.authenticate("ats-orchestrator", "s" * 40)
    assert not clients.authenticate("agent-knowledge-graph", "")


def test_policy_listing_a_user_audience_is_refused_at_startup():
    with pytest.raises(RuntimeError):
        AuthSettings(oauth_client_audiences="ats-orchestrator=ats-api").client_audiences()


# ------------------------------------------------------------------------------------------------ discovery
async def test_jwks_publishes_only_public_es256_keys(service):
    response = await service.client.get("/.well-known/jwks.json")
    assert response.status_code == 200 and "max-age" in response.headers["cache-control"]
    keys = response.json()["keys"]
    assert len(keys) == 1
    key = keys[0]
    assert set(key) == {"kty", "crv", "x", "y", "kid", "alg", "use"}               # no private "d"
    assert (key["kty"], key["crv"], key["alg"], key["use"]) == ("EC", "P-256", "ES256", "sig")
    assert key["kid"] == jwk_thumbprint(key)


async def test_openid_configuration_points_at_the_jwks_and_token_endpoint(service):
    document = (await service.client.get("/.well-known/openid-configuration")).json()
    assert document["issuer"] == TEST_ISSUER
    assert document["jwks_uri"].endswith("/.well-known/jwks.json")
    assert document["token_endpoint"].endswith("/oauth/token")
    assert document["grant_types_supported"] == ["client_credentials"]


# ------------------------------------------------------------------------------------------------ keys / rotation
def test_signing_key_must_be_ec_p256():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    p384 = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    with pytest.raises(ValueError):
        SigningKey.from_pem(p384)


def test_same_pem_gives_the_same_kid_and_one_line_pem_with_escaped_newlines_works():
    pem = pem_of_new_key()
    one_line = pem.strip().replace("\n", "\\n")              # how the PEM sits in a local .env file
    first = KeyRing.from_settings(AuthSettings(auth_signing_key=pem))
    second = KeyRing.from_settings(AuthSettings(auth_signing_key=one_line))
    assert first.current.kid == second.current.kid


async def test_rotation_keeps_tokens_of_the_previous_key_valid():
    old_pem, new_pem = pem_of_new_key(), pem_of_new_key()
    before = TokenIssuer.from_settings(settings := AuthSettings(auth_signing_key=old_pem, auth_issuer=TEST_ISSUER),
                                       KeyRing.from_settings(settings))
    old_token = before.service_token("ats-orchestrator", "agent-skill").value

    rotated = AuthSettings(auth_signing_key=new_pem, auth_signing_key_previous=old_pem, auth_issuer=TEST_ISSUER)
    after = TokenIssuer.from_settings(rotated, KeyRing.from_settings(rotated))
    assert [k["kid"] for k in after.keys.jwks()["keys"]] == [after.keys.current.kid, before.keys.current.kid]

    async def published():
        return after.keys.jwks()

    verifier = JwksVerifier(published, TEST_ISSUER)
    assert await verify_service_token(verifier, old_token, "agent-skill") == "ats-orchestrator"
    new_token = after.service_token("ats-orchestrator", "agent-skill").value
    assert await verify_service_token(verifier, new_token, "agent-skill") == "ats-orchestrator"
