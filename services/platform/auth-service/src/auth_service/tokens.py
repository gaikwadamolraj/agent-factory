"""Issue and verify the auth-service tokens (all ES256, signed with the KeyRing's current key).

* access   aud=ats-api        sub=user id, cid=client id, role, username,    verified by every service (JWKS)
                              authz_ver (user's authz version, agent_common.authz)
* refresh  aud=ats-refresh    sub=user id, jti (allow-listed in Redis)        verified only here
* service  aud=<service>      sub=calling service, kind=service               verified by the called service

Verification here uses the same `JwksVerifier` as every other service, fed with the KeyRing's own public keys
(no network call), so the auth-service and the services accept exactly the same tokens.
"""
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

from agent_common.constants import CLAIM_AUTHZ_VERSION, JWT_AUDIENCE_REFRESH, JWT_AUDIENCE_USER, TOKEN_KIND_SERVICE
from agent_common.errors import AppError, Errors
from agent_common.security import JwksVerifier, UserClaims, verify_user_token
from auth_service.config import AuthSettings
from auth_service.signing import KeyRing


@dataclass(frozen=True)
class IssuedToken:
    value: str
    jti: str
    expires_in: int          # seconds


@dataclass(frozen=True)
class RefreshClaims:
    user_id: int
    jti: str


class TokenIssuer:
    """Creates tokens with the configured lifetimes and verifies access/refresh tokens locally."""

    def __init__(self, keys: KeyRing, issuer: str, access_ttl_s: int, refresh_ttl_s: int, service_ttl_s: int,
                 leeway_s: int = 30):
        self.keys, self.issuer = keys, issuer
        self.access_ttl_s, self.refresh_ttl_s, self.service_ttl_s = access_ttl_s, refresh_ttl_s, service_ttl_s
        # Own keys never change while the process runs: load them once, never refetch.
        self.verifier = JwksVerifier(keys.fetch_jwks, issuer, leeway_s=leeway_s, min_refetch_interval_s=math.inf)

    @classmethod
    def from_settings(cls, settings: AuthSettings, keys: KeyRing) -> "TokenIssuer":
        return cls(keys, settings.auth_issuer, access_ttl_s=settings.access_token_minutes * 60,
                   refresh_ttl_s=settings.refresh_token_days * 24 * 3600,
                   service_ttl_s=settings.service_token_minutes * 60, leeway_s=settings.auth_leeway_s)

    def access_token(self, user: UserClaims) -> IssuedToken:
        """Access token (aud=ats-api) for a signed-in user; `user.authz_ver` must be the current authz version."""
        return self._issue({"aud": JWT_AUDIENCE_USER, "sub": str(user.user_id), "cid": user.client_id,
                            "role": user.role, "username": user.username, CLAIM_AUTHZ_VERSION: user.authz_ver},
                           self.access_ttl_s)

    def refresh_token(self, user_id: int) -> IssuedToken:
        """Refresh token (aud=ats-refresh); the caller allow-lists its jti (RefreshTokenStore)."""
        return self._issue({"aud": JWT_AUDIENCE_REFRESH, "sub": str(user_id)}, self.refresh_ttl_s)

    def service_token(self, client_id: str, audience: str) -> IssuedToken:
        """Service token of `client_id` for calls to `audience` (client_credentials grant)."""
        return self._issue({"aud": audience, "sub": client_id, "kind": TOKEN_KIND_SERVICE}, self.service_ttl_s)

    async def verify_access(self, token: str) -> UserClaims:
        """Claims of a valid access token. Raises AppError (AUTH-EXP-0001 / AUTH-INV-0001)."""
        return await verify_user_token(self.verifier, token)

    async def verify_refresh(self, token: str) -> RefreshClaims:
        """Claims of a valid refresh token. Raises AppError (AUTH-EXP-0001 / AUTH-INV-0001)."""
        claims = await self.verifier.verify(token, JWT_AUDIENCE_REFRESH)
        try:
            return RefreshClaims(int(claims["sub"]), str(claims["jti"]))
        except (KeyError, ValueError):
            raise AppError(Errors.AUTH_TOKEN_INVALID) from None

    def _issue(self, claims: dict[str, Any], ttl_s: int) -> IssuedToken:
        now, jti = int(time.time()), uuid.uuid4().hex
        token = self.keys.sign({"iss": self.issuer, "iat": now, "exp": now + ttl_s, "jti": jti, **claims})
        return IssuedToken(token, jti, ttl_s)
