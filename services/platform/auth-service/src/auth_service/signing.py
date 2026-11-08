"""Signing keys of the auth-service (EC P-256, ES256) and the public key set it publishes (JWKS).

Rotation: put the old PEM into AUTH_SIGNING_KEY_PREVIOUS and a new PEM into AUTH_SIGNING_KEY, restart. New tokens
are signed with the new key; the JWKS lists both public keys, so tokens signed before the rotation stay valid
until they expire. Remove the previous key after the longest token lifetime (refresh tokens: REFRESH_TOKEN_DAYS).
"""
from dataclasses import dataclass
from typing import Any, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from agent_common.constants import JWT_ALGORITHM
from agent_common.security.keys import public_jwk
from auth_service.config import AuthSettings


@dataclass(frozen=True)
class SigningKey:
    """One private key and its public JWK (kid = RFC 7638 thumbprint)."""

    private_key: ec.EllipticCurvePrivateKey
    public_jwk: dict[str, str]

    @property
    def kid(self) -> str:
        return self.public_jwk["kid"]

    @classmethod
    def from_pem(cls, pem: str) -> "SigningKey":
        """Load an unencrypted PEM private key. Raises ValueError unless it is an EC P-256 key."""
        private_key = load_pem_private_key(pem.encode("utf-8"), password=None)
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise ValueError("The signing key must be an EC P-256 private key.")
        return cls(private_key, public_jwk(private_key.public_key()))   # public_jwk rejects other curves


class KeyRing:
    """The current signing key plus, during a rotation, the previous one (published, never used to sign)."""

    def __init__(self, current: SigningKey, previous: Optional[SigningKey] = None):
        self.current = current
        self.previous = previous if previous and previous.kid != current.kid else None

    @classmethod
    def from_settings(cls, settings: AuthSettings) -> "KeyRing":
        """Keys from AUTH_SIGNING_KEY / AUTH_SIGNING_KEY_PREVIOUS. Raises RuntimeError/ValueError when invalid."""
        previous_pem = settings.previous_signing_key_pem()
        return cls(SigningKey.from_pem(settings.signing_key_pem()),
                   SigningKey.from_pem(previous_pem) if previous_pem else None)

    def sign(self, claims: dict[str, Any]) -> str:
        """ES256 token of `claims`, signed with the current key (header kid = its thumbprint)."""
        return jwt.encode(claims, self.current.private_key, algorithm=JWT_ALGORITHM,
                          headers={"kid": self.current.kid})

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """The public key set: current key first, then the previous key during a rotation."""
        keys = [self.current] + ([self.previous] if self.previous else [])
        return {"keys": [key.public_jwk for key in keys]}

    async def fetch_jwks(self) -> dict[str, list[dict[str, str]]]:
        """JwksFetcher for the auth-service's own verifier: the same keys, without a network call."""
        return self.jwks()
