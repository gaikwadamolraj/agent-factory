"""Test helper: signs tokens like the auth-service with an in-memory EC key and serves the matching JWKS.
For tests only - services' tests use it so they never need a running auth-service.

    issuer = LocalTokenIssuer()
    verifier = issuer.verifier()                                   # JwksVerifier backed by issuer.fetch_jwks
    token = issuer.user_token(user_id=7, client_id=1)
    token = issuer.service_token("ats-orchestrator", "agent-skill")
"""
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from agent_common.constants import CLAIM_AUTHZ_VERSION, JWT_ALGORITHM, JWT_AUDIENCE_USER, TOKEN_KIND_SERVICE
from agent_common.security.jwks import JwksVerifier
from agent_common.security.keys import public_jwk

TEST_ISSUER = "http://auth-service.test"


class LocalTokenIssuer:
    """An auth-service stand-in: one fresh P-256 key, its JWKS and token factories."""

    def __init__(self, issuer: str = TEST_ISSUER):
        self.issuer = issuer
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.jwk = public_jwk(self.private_key.public_key())
        self.jwks_fetches = 0

    @property
    def kid(self) -> str:
        return self.jwk["kid"]

    async def fetch_jwks(self) -> dict:
        """JwksFetcher: the public key of this issuer (counts the calls, for cache tests)."""
        self.jwks_fetches += 1
        return {"keys": [self.jwk]}

    def verifier(self, **options: Any) -> JwksVerifier:
        """A JwksVerifier that trusts this issuer (options: cache_ttl_s, leeway_s, min_refetch_interval_s, clock)."""
        return JwksVerifier(self.fetch_jwks, self.issuer, **options)

    def sign(self, claims: dict[str, Any], ttl_s: int = 300) -> str:
        """ES256 token with iss/iat/exp/jti added to `claims` (negative ttl_s = already expired)."""
        now = int(time.time())
        payload = {"iss": self.issuer, "iat": now, "exp": now + ttl_s, "jti": uuid.uuid4().hex, **claims}
        return jwt.encode(payload, self.private_key, algorithm=JWT_ALGORITHM, headers={"kid": self.kid})

    def user_token(self, user_id: int = 1, client_id: int = 1, role: str = "recruiter", username: str = "user",
                   ttl_s: int = 300, authz_ver: int = 0) -> str:
        """Access token (aud=ats-api) like the one the auth-service issues at login."""
        return self.sign({"aud": JWT_AUDIENCE_USER, "sub": str(user_id), "cid": client_id, "role": role,
                          "username": username, CLAIM_AUTHZ_VERSION: authz_ver}, ttl_s)

    def service_token(self, caller: str, audience: str, ttl_s: int = 300) -> str:
        """Service token (client_credentials) of `caller` for `audience`."""
        return self.sign({"aud": audience, "sub": caller, "kind": TOKEN_KIND_SERVICE}, ttl_s)
