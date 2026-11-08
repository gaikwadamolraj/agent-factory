"""Token verification and service tokens for every service. Only the auth-service signs tokens (ES256).

Token kinds, never interchangeable (different audience):
* access tokens   aud=ats-api        issued at login/refresh: sub=user id, cid=client id, role, username,
                                     authz_ver (rejected once the user's authz version moved on, agent_common.authz)
* refresh tokens  aud=ats-refresh    only the auth-service reads them (rotated, allow-listed in Redis)
* service tokens  aud=<service name> client_credentials grant of a calling service: sub=caller, kind=service

    verifier = build_jwks_verifier(settings)                       # every service (middleware)
    claims = await verify_user_token(verifier, token)              # -> UserClaims
    caller = await verify_service_token(verifier, token, "agent-skill")

    tokens = build_service_token_provider(settings)                # services that call other services
    token = await tokens.token("agent-skill")
"""
from dataclasses import dataclass
from typing import Any

from agent_common.config import CommonSettings
from agent_common.constants import CLAIM_AUTHZ_VERSION, JWT_AUDIENCE_USER, TOKEN_KIND_SERVICE
from agent_common.errors import AppError, Errors
from agent_common.security.jwks import HttpJwksFetcher, JwksVerifier
from agent_common.security.service_tokens import (HttpTokenFetcher, ServiceTokenAuth, ServiceTokenProvider,
                                                TokenRequestRefused)


@dataclass(frozen=True)
class UserClaims:
    """Who is calling, from a verified access token."""

    user_id: int
    client_id: int
    role: str
    username: str
    authz_ver: int = 0      # authz version at issue time; compared with the current one (agent_common.authz)


def user_claims_from(claims: dict[str, Any]) -> UserClaims:
    """UserClaims of verified access-token claims. Raises AppError(AUTH-INV-0001) when a field is missing/invalid
    or the token is a service token."""
    try:
        if claims.get("kind") == TOKEN_KIND_SERVICE:
            raise ValueError("service token used as a user token")
        return UserClaims(int(claims["sub"]), int(claims["cid"]), str(claims.get("role", "")),
                          str(claims.get("username", "")), int(claims.get(CLAIM_AUTHZ_VERSION, 0)))
    except (KeyError, TypeError, ValueError):
        raise AppError(Errors.AUTH_TOKEN_INVALID) from None


async def verify_user_token(verifier: JwksVerifier, token: str) -> UserClaims:
    """Verify a user access token (aud=ats-api). Raises AppError (AUTH-EXP-0001 / AUTH-INV-0001 / SYS-UNA-0001)."""
    return user_claims_from(await verifier.verify(token, JWT_AUDIENCE_USER))


async def verify_service_token(verifier: JwksVerifier, token: str, audience: str) -> str:
    """Verify a service token for `audience` (this service's name); returns the caller's service name.
    Raises AppError (AUTH-EXP-0001 / AUTH-INV-0001 / SYS-UNA-0001); user tokens are rejected."""
    claims = await verifier.verify(token, audience)
    if claims.get("kind") != TOKEN_KIND_SERVICE:
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    return str(claims["sub"])


def build_jwks_verifier(settings: CommonSettings) -> JwksVerifier:
    """The verifier of this service: keys from AUTH_JWKS_URL, issuer AUTH_ISSUER, limits from the settings."""
    fetcher = HttpJwksFetcher(settings.auth_jwks_url, settings.auth_http_timeout_s, settings.auth_http_retry_attempts)
    return JwksVerifier(fetcher, settings.auth_issuer, cache_ttl_s=settings.auth_jwks_cache_ttl_s,
                        leeway_s=settings.auth_leeway_s, min_refetch_interval_s=settings.auth_jwks_min_refetch_s)


def build_service_token_provider(settings: CommonSettings) -> ServiceTokenProvider:
    """Service tokens for this service (client id = SERVICE_NAME, secret = SERVICE_CLIENT_SECRET).
    Raises RuntimeError when the secret is not configured."""
    fetcher = HttpTokenFetcher(settings.auth_token_url, settings.service_name, settings.require_service_client_secret(),
                               settings.auth_http_timeout_s, settings.auth_http_retry_attempts)
    return ServiceTokenProvider(fetcher, settings.service_token_refresh_ratio)


__all__ = ["HttpJwksFetcher", "HttpTokenFetcher", "JwksVerifier", "ServiceTokenAuth", "ServiceTokenProvider",
           "TokenRequestRefused", "UserClaims", "build_jwks_verifier", "build_service_token_provider",
           "user_claims_from", "verify_service_token", "verify_user_token"]
