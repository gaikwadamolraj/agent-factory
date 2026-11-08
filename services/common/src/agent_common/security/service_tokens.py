"""Service-to-service tokens from the auth-service (OAuth2 client_credentials), cached per audience.

    tokens = ServiceTokenProvider(HttpTokenFetcher(settings.auth_token_url, "ats-orchestrator", secret))
    token = await tokens.token("agent-skill")      # Authorization: Bearer <token> on calls to agent-skill

* One token per audience (= the called service), reused until `refresh_ratio` (default 80%) of its lifetime has
  passed, so a service asks the auth-service only every few minutes per target, not per call.
* Concurrent callers for the same audience share one request (per-audience lock).
* The auth-service decides which caller may get a token for which audience; a refused request raises
  AUTH-FBD-0002, an unreachable auth-service SYS-UNA-0001 (retryable).
"""
import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.resilience import BreakerConfig, CircuitOpenError, RetryPolicy, get_breaker, resilient_call

log = get_logger(__name__)

# audience -> (access token, lifetime in seconds)
TokenFetcher = Callable[[str], Awaitable[tuple[str, int]]]
TOKEN_UNAVAILABLE_RETRY_AFTER_S = 5
_REFUSED_STATUSES = frozenset({400, 401, 403, 429})


class TokenRequestRefused(RuntimeError):
    """The auth-service answered with a client error (bad secret, audience not allowed, locked): never retried."""

    def __init__(self, status: int):
        super().__init__(f"token request refused with HTTP {status}")
        self.status = status


class HttpTokenFetcher:
    """POSTs a client_credentials grant to the auth-service token endpoint (resilient: timeout, retry, breaker)."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, timeout_s: float = 5.0,
                 retry_attempts: int = 2,
                 breaker: BreakerConfig = BreakerConfig(failure_threshold=3, reset_timeout_s=15)):
        self._url, self._client_id, self._client_secret = token_url, client_id, client_secret
        self._timeout_s, self._retry_attempts = timeout_s, retry_attempts
        self._breaker = get_breaker("auth.token", breaker)

    async def __call__(self, audience: str) -> tuple[str, int]:
        """(token, lifetime seconds) for `audience`. Raises AppError: AUTH-FBD-0002 refused, SYS-UNA-0001 otherwise."""
        try:
            payload = await resilient_call(
                lambda: self._post(audience), op="auth.token", timeout_s=self._timeout_s,
                retry=RetryPolicy(attempts=self._retry_attempts, retry_on=(httpx.TransportError, TimeoutError)),
                breaker=self._breaker, is_failure=lambda e: not isinstance(e, TokenRequestRefused))
            return str(payload["access_token"]), int(payload["expires_in"])
        except asyncio.CancelledError:
            raise
        except TokenRequestRefused as e:
            log_event(log, "auth.service_token_refused", audience=audience, status=e.status)
            raise AppError(Errors.SERVICE_FORBIDDEN) from None
        except CircuitOpenError as e:
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=e.retry_after_s) from None
        except Exception as e:      # transport error after retries, timeout, malformed answer: hide internals
            log_event(log, "auth.service_token_failed", audience=audience, error_type=type(e).__name__)
            raise AppError(Errors.UPSTREAM_UNAVAILABLE, retry_after=TOKEN_UNAVAILABLE_RETRY_AFTER_S) from None

    async def _post(self, audience: str) -> dict:
        form = {"grant_type": "client_credentials", "client_id": self._client_id,
                "client_secret": self._client_secret, "audience": audience}
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.post(self._url, data=form)
        if response.status_code in _REFUSED_STATUSES:
            raise TokenRequestRefused(response.status_code)
        response.raise_for_status()
        return response.json()


@dataclass(frozen=True)
class _CachedToken:
    value: str
    renew_at: float         # monotonic time after which a new token is fetched


class ServiceTokenProvider:
    """Cached service tokens per audience (see the module docstring)."""

    def __init__(self, fetch_token: TokenFetcher, refresh_ratio: float = 0.8,
                 clock: Callable[[], float] = time.monotonic):
        if not 0 < refresh_ratio < 1:
            raise ValueError("refresh_ratio must be between 0 and 1.")
        self._fetch_token, self._refresh_ratio, self._clock = fetch_token, refresh_ratio, clock
        self._cache: dict[str, _CachedToken] = {}       # bounded: one entry per configured target service
        self._locks: dict[str, asyncio.Lock] = {}

    async def token(self, audience: str) -> str:
        """A valid token for `audience` (cached or new). Raises AppError when the auth-service refuses or is down."""
        cached = self._fresh(audience)
        if cached:
            return cached
        async with self._locks.setdefault(audience, asyncio.Lock()):
            cached = self._fresh(audience)        # another caller may have fetched it while we waited
            if cached:
                return cached
            value, lifetime_s = await self._fetch_token(audience)
            self._cache[audience] = _CachedToken(value, self._clock() + lifetime_s * self._refresh_ratio)
            return value

    def _fresh(self, audience: str) -> str | None:
        cached = self._cache.get(audience)
        return cached.value if cached and self._clock() < cached.renew_at else None


class ServiceTokenAuth(httpx.Auth):
    """httpx auth that puts a (cached) service token for `audience` on every request.

    Long MCP sessions outlive one token; asking the provider per request keeps every request authorised.
    """

    def __init__(self, provider: ServiceTokenProvider, audience: str):
        self._provider, self._audience = provider, audience

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._provider.token(self._audience)}"
        yield request
