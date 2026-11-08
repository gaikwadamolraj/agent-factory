"""Who is calling a tool, from a verified token. Both front doors reduce to one of these before authorization.

* UserPrincipal     a human (admin UI / external client) with a verified access token (aud ats-api). Authorized by
                    the IAM policy engine (the human layer).
* ServicePrincipal  another service/agent with a verified service token (aud registry-service). Authorized by the
                    machine layer (ALLOWED_CALLERS + per-tool TOOL_POLICY), the same check whether it arrives over
                    the MCP front door or the REST wrapper.
"""
from dataclasses import dataclass

from agent_common.security import UserClaims


@dataclass(frozen=True)
class UserPrincipal:
    """A signed-in human. `assume_client_id`, when set, is the client a super admin chose to act in (audited)."""

    claims: UserClaims
    assume_client_id: int | None = None

    @property
    def is_service(self) -> bool:
        return False

    @property
    def label(self) -> str:
        """Low-cardinality-friendly identifier for logs/metrics and the rate-limit key."""
        return f"user:{self.claims.user_id}"

    @property
    def effective_client_id(self) -> int:
        """The client whose data this call touches: the chosen client for a super admin, else the token's client."""
        return self.assume_client_id or self.claims.client_id


@dataclass(frozen=True)
class ServicePrincipal:
    """A calling service/agent, identified by its service name from a verified service token."""

    caller: str

    @property
    def is_service(self) -> bool:
        return True

    @property
    def label(self) -> str:
        return f"service:{self.caller}"
