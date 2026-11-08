"""registry-service -> auth-service contract for identity writes (delegated admin, docs/impl_registry_service.md section 6b).

Ownership split: registry-service owns clients, projects, groups, policies, skills and notifications; the auth-service
owns users, credentials and invites (identity). So "invite a user", "create a user", "set a role" and "deactivate a
user" are delegated here over a service-to-service call: a short-lived service token (aud `auth-service`, caller
`registry-service`, verified by the auth-service with a tool/caller allow-list) on an HTTP request to the auth-service's
`/api/v1/identity/*` endpoints. Every outbound call goes through agent_common.resilience (timeout + bounded retry +
named breaker), and identity failures surface as USR-UNA-0001 (never a stack trace).

Transport is plain HTTP (REST) here; the same contract could ride MCP later without changing callers.
"""
from typing import Any, Optional

import httpx

from agent_common.constants import HEADER_REQUEST_ID
from agent_common.context import current_context
from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from agent_common.resilience import BreakerConfig, RetryPolicy, get_breaker, resilient_call
from agent_common.security import ServiceTokenAuth, ServiceTokenProvider

from registry_service import metrics
from registry_service.errors import FactoryErrors

log = get_logger(__name__)


class IdentityClient:
    """Typed client for the auth-service identity endpoints, authenticated with an registry-service service token."""

    def __init__(self, base_url: str, audience: str, tokens: ServiceTokenProvider, timeout_s: float = 5.0,
                 retry_attempts: int = 2):
        self._base_url = base_url.rstrip("/")
        self._auth = ServiceTokenAuth(tokens, audience)
        self._timeout_s, self._retry_attempts = timeout_s, retry_attempts
        self._breaker = get_breaker("registry-service.identity",
                                    BreakerConfig(failure_threshold=3, reset_timeout_s=15))

    async def create_user(self, *, client_id: int, username: str, email: Optional[str], role: str,
                          active: bool = True) -> dict:
        """Create a user (active or pending) in a client. Returns the created user as the auth-service exposes it."""
        return await self._post("/api/v1/identity/users", {"clientId": client_id, "username": username,
                                                           "email": email, "role": role, "active": active}, "create_user")

    async def set_role(self, *, client_id: int, user_id: int, role: str) -> dict:
        """Change a user's role; the auth-service bumps the user's authz version so it applies at once."""
        return await self._patch(f"/api/v1/identity/users/{user_id}", {"clientId": client_id, "role": role},
                                 "set_role")

    async def deactivate_user(self, *, client_id: int, user_id: int) -> dict:
        """Deactivate a user (cannot sign in or refresh); the auth-service bumps the user's authz version."""
        return await self._patch(f"/api/v1/identity/users/{user_id}", {"clientId": client_id, "active": False},
                                 "deactivate_user")

    async def create_invite(self, *, client_id: int, email: str, role: str, group_ids: list[int],
                            project_ids: list[int], invited_by: int) -> dict:
        """Create a signed, expiring, single-use invite + a pending user. Returns {invite..., inviteLink}."""
        return await self._post("/api/v1/identity/invites",
                                {"clientId": client_id, "email": email, "role": role, "groupIds": group_ids,
                                 "projectIds": project_ids, "invitedBy": invited_by}, "create_invite")

    async def list_invites(self, *, client_id: int) -> dict:
        """Pending invites of a client."""
        return await self._get("/api/v1/identity/invites", {"clientId": client_id}, "list_invites")

    async def revoke_invite(self, *, client_id: int, invite_id: str) -> dict:
        """Revoke a pending invite."""
        return await self._post(f"/api/v1/identity/invites/{invite_id}/revoke", {"clientId": client_id},
                                "revoke_invite")

    # ----------------------------------------------------------------- transport
    async def _get(self, path: str, params: dict[str, Any], op: str) -> dict:
        return await self._call("GET", path, op, params=params)

    async def _post(self, path: str, body: dict[str, Any], op: str) -> dict:
        return await self._call("POST", path, op, json=body)

    async def _patch(self, path: str, body: dict[str, Any], op: str) -> dict:
        return await self._call("PATCH", path, op, json=body)

    async def _call(self, method: str, path: str, op: str, **kwargs) -> dict:
        """One resilient HTTP call to the auth-service, propagating the request id and the service token."""
        url = f"{self._base_url}{path}"
        headers = {HEADER_REQUEST_ID: current_context().request_id} if current_context().request_id else {}

        async def do() -> httpx.Response:
            async with httpx.AsyncClient(timeout=self._timeout_s, auth=self._auth) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                return response

        try:
            response = await resilient_call(
                do, op=f"identity.{op}", timeout_s=self._timeout_s,
                retry=RetryPolicy(attempts=self._retry_attempts, retry_on=(httpx.TransportError, TimeoutError)),
                breaker=self._breaker)
            metrics.identity_calls_total.add(1, {"op": op, "outcome": "ok"})
            return response.json()
        except AppError:
            raise
        except Exception as e:      # transport/HTTP error: hide internals behind USR-UNA-0001
            metrics.identity_calls_total.add(1, {"op": op, "outcome": "error"})
            log_event(log, "identity.call_failed", op=op, error_type=type(e).__name__)
            raise AppError(FactoryErrors.IDENTITY_UPSTREAM) from None
