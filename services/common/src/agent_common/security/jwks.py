"""Verify tokens signed by the auth-service (ES256) with its published public keys (JWKS).

    verifier = JwksVerifier(HttpJwksFetcher(settings.auth_jwks_url), issuer=settings.auth_issuer)
    claims = await verifier.verify(token, audience="ats-api")      # dict of verified claims, or AppError

* The keys are fetched once and cached. A token whose `kid` is not in the cache (the auth-service rotated its key)
  triggers one refetch, at most once per `min_refetch_interval_s`, so tokens with random kids cannot flood the
  auth-service. An optional `cache_ttl_s` also refreshes the cache periodically (default None: never, so a removed
  key stays trusted by a running service until its restart or the next unknown kid).
* Every fetch goes through `agent_common.resilience` (timeout, bounded retry, circuit breaker "auth.jwks").
* When a refetch fails, the cached keys keep working; without any cached key, verification answers
  SYS-UNA-0001 (retryable) instead of rejecting the token.
* Only ES256 is accepted; issuer, audience, expiry and issued-at are checked with a small leeway for clock skew.
"""
import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import httpx
import jwt

from agent_common.constants import JWT_ALGORITHM
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.resilience import BreakerConfig, CircuitOpenError, RetryPolicy, get_breaker, resilient_call

log = get_logger(__name__)

# Returns the JWKS document: {"keys": [jwk, ...]}.
JwksFetcher = Callable[[], Awaitable[dict]]
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]
JWKS_UNAVAILABLE_RETRY_AFTER_S = 5


class HttpJwksFetcher:
    """Reads the JWKS document from the auth-service over HTTP (resilient: timeout, retry, breaker)."""

    def __init__(self, url: str, timeout_s: float = 5.0, retry_attempts: int = 2,
                 breaker: BreakerConfig = BreakerConfig(failure_threshold=3, reset_timeout_s=15)):
        self.url, self.timeout_s, self.retry_attempts = url, timeout_s, retry_attempts
        self._breaker = get_breaker("auth.jwks", breaker)

    async def __call__(self) -> dict:
        """The JWKS document. Raises the last transport/HTTP error, CallTimeoutError or CircuitOpenError."""
        return await resilient_call(self._get, op="auth.jwks", timeout_s=self.timeout_s,
                                    retry=RetryPolicy(attempts=self.retry_attempts,
                                                      retry_on=(httpx.TransportError, TimeoutError)),
                                    breaker=self._breaker)

    async def _get(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            return response.json()


def _parse_jwks(document: dict) -> dict[str, jwt.PyJWK]:
    """kid -> key for every usable ES256 key of a JWKS document; malformed entries are skipped (and logged)."""
    keys: dict[str, jwt.PyJWK] = {}
    for entry in document.get("keys", []) if isinstance(document, dict) else []:
        kid = entry.get("kid") if isinstance(entry, dict) else None
        if not kid or entry.get("kty") != "EC" or entry.get("alg", JWT_ALGORITHM) != JWT_ALGORITHM:
            continue
        try:
            keys[kid] = jwt.PyJWK(entry, algorithm=JWT_ALGORITHM)
        except jwt.PyJWTError:
            log_event(log, "auth.jwks_key_skipped", kid=str(kid)[:64])
    return keys


def _kid_of(token: str) -> str:
    """The `kid` of an ES256 token header. Raises AppError(AUTH-INV-0001) for anything else."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise AppError(Errors.AUTH_TOKEN_INVALID) from None
    kid = header.get("kid")
    if header.get("alg") != JWT_ALGORITHM or not isinstance(kid, str) or not kid:
        raise AppError(Errors.AUTH_TOKEN_INVALID)
    return kid


class JwksVerifier:
    """Verifies auth-service tokens with cached public keys (see the module docstring for the refresh rules)."""

    def __init__(self, fetch_jwks: JwksFetcher, issuer: str, cache_ttl_s: Optional[float] = None, leeway_s: int = 30,
                 min_refetch_interval_s: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self._fetch_jwks, self.issuer = fetch_jwks, issuer
        self._cache_ttl_s, self._leeway_s, self._min_refetch_s = cache_ttl_s, leeway_s, min_refetch_interval_s
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: Optional[float] = None      # last successful fetch
        self._attempted_at: Optional[float] = None    # last fetch attempt (successful or not)
        self._lock = asyncio.Lock()

    async def verify(self, token: str, audience: str) -> dict[str, Any]:
        """Verified claims of `token` for `audience`. Raises AppError: AUTH-EXP-0001 (expired), AUTH-INV-0001
        (bad signature, issuer, audience, unknown key, malformed) or SYS-UNA-0001 (no keys and the auth-service is
        unreachable)."""
        key = await self._key_for(_kid_of(token))
        try:
            return jwt.decode(token, key.key, algorithms=[JWT_ALGORITHM], audience=audience, issuer=self.issuer,
                              leeway=self._leeway_s, options={"require": REQUIRED_CLAIMS})
        except jwt.ExpiredSignatureError:
            raise AppError(Errors.AUTH_TOKEN_EXPIRED) from None
        except jwt.PyJWTError:
            raise AppError(Errors.AUTH_TOKEN_INVALID) from None

    async def _key_for(self, kid: str) -> jwt.PyJWK:
        # Fetch on first use, on a new kid (key rotation) and, if configured, when the cache is old - rate-limited.
        if (self._cache_is_stale() or kid not in self._keys) and self._may_refetch():
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            log_event(log, "auth.unknown_kid")
            raise AppError(Errors.AUTH_TOKEN_INVALID)
        return key

    def _cache_is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        return self._cache_ttl_s is not None and self._clock() - self._fetched_at >= self._cache_ttl_s

    def _may_refetch(self) -> bool:
        return self._attempted_at is None or self._clock() - self._attempted_at >= self._min_refetch_s

    async def _refresh(self) -> None:
        """Fetch the keys once; concurrent callers wait for the same fetch instead of starting their own."""
        async with self._lock:
            if not self._may_refetch():         # another request refreshed while we waited for the lock
                return
            self._attempted_at = self._clock()
            try:
                keys = _parse_jwks(await self._fetch_jwks())
            except asyncio.CancelledError:
                raise
            except Exception as e:              # unreachable / bad response: keep the cached keys if we have any
                log_event(log, "auth.jwks_fetch_failed", error_type=type(e).__name__, cached_keys=len(self._keys))
                if not self._keys:
                    retry_after = e.retry_after_s if isinstance(e, CircuitOpenError) else JWKS_UNAVAILABLE_RETRY_AFTER_S
                    raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=retry_after) from None
                return
            self._keys, self._fetched_at = keys, self._attempted_at
            log_event(log, "auth.jwks_refreshed", keys=len(keys))
