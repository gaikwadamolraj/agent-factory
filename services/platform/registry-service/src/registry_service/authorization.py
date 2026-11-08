"""The authorization layer: one decision function both front doors call, default deny.

A `ToolSpec` describes one operation: its request model, its handler, and how to authorize it. `authorize()` is the
single decision point:

* a ServicePrincipal (MCP front door, or a service token on the REST wrapper) is checked against the machine layer -
  the per-caller TOOL_POLICY allow-list - the same check wherever it arrives (defence in depth with the MCP
  middleware, and the only check on the REST path, so there is one authority);
* a UserPrincipal (the admin UI / an external client) is checked against the IAM policy engine (the human layer):
  the tool's required action + resource are evaluated for the subject, explicit deny wins, else an allow must match,
  else default deny. A tool with no `access` builder and not marked self-scoped is refused (nothing is implicit);
* self-scoped tools (a user reading or acting on their own notifications) skip the engine: the handler scopes
  strictly to the actor, so no policy is needed and none implicitly grants cross-user access.
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.models import ApiModel
from agent_common.policy import AccessRequest, Subject

from registry_service import metrics
from registry_service.principals import ServicePrincipal, UserPrincipal

if TYPE_CHECKING:
    from registry_service.container import Container

log = get_logger(__name__)

Principal = UserPrincipal | ServicePrincipal


@dataclass
class ToolContext:
    """Everything a tool handler needs: a tenant-scoped DB session, the actor, the resolved client, shared services.

    `session` is already scoped (RLS GUC set) to `client_id`, so every read/write the handler makes is isolated to
    that client even if a filter is missed. `subject` is the policy Subject for a user principal (None for a service
    principal). `container` exposes the redis client, policy engine, notifier, identity client and catalogue.
    """

    principal: Principal
    actor_id: Optional[int]
    client_id: int
    session: AsyncSession
    container: "Container"
    subject: Optional[Subject] = None


# Builds the IAM access request (action + resource + the resource attributes conditions compare) for a user call.
AccessBuilder = Callable[["ToolContext", ApiModel], Awaitable[AccessRequest]]
# A tool handler: runs the business logic against the tenant-scoped session, returns a JSON-ready dict.
ToolHandler = Callable[["ToolContext", ApiModel], Awaitable[dict]]


@dataclass(frozen=True)
class ToolSpec:
    """One operation, defined once and reached from both front doors."""

    name: str
    description: str
    request_model: type[ApiModel]
    handler: ToolHandler
    mutating: bool
    access: Optional[AccessBuilder] = None          # None only when self_scoped (else default deny)
    self_scoped: bool = False                        # the caller's own data; skips the IAM engine
    require_super_admin: bool = False                # cross-tenant / platform-owner only
    # For the report / admin UI: the action + resource template this tool maps to (documentation, not evaluated).
    action: str = ""
    resource: str = ""


def _service_allowed(container: "Container", caller: str, tool_name: str) -> bool:
    """Machine layer: is `caller` allowed to use `tool_name` per TOOL_POLICY (same map the MCP middleware uses)?"""
    allowed = container.tool_policy.get(caller, set())
    return "*" in allowed or tool_name in allowed


async def authorize(ctx: ToolContext, spec: ToolSpec, request: ApiModel) -> None:
    """Raise AppError unless the principal may run this tool. Default deny for anything unmapped."""
    principal = ctx.principal
    if isinstance(principal, ServicePrincipal):
        allowed = _service_allowed(ctx.container, principal.caller, spec.name)
        metrics.authz_decisions_total.add(1, {"action": spec.action or spec.name,
                                              "outcome": "allow" if allowed else "deny"})
        if not allowed:
            log_event(log, "factory.service_denied", caller=principal.caller, tool=spec.name)
            raise AppError(Errors.SERVICE_FORBIDDEN)
        return

    # User principal: the human IAM layer.
    if spec.self_scoped:
        return                                  # the handler scopes to the actor; no policy needed
    if spec.access is None:
        raise AppError(Errors.AUTH_FORBIDDEN)   # unmapped tool: refuse (nothing is exposed implicitly)
    if spec.require_super_admin and not (ctx.subject and ctx.subject.is_super_admin):
        log_event(log, "factory.super_admin_required", tool=spec.name, user_id=principal.claims.user_id)
        raise AppError(Errors.AUTH_FORBIDDEN)

    access_request = await spec.access(ctx, request)
    decision = await ctx.container.policy_engine.authorize(ctx.subject, access_request)
    metrics.authz_decisions_total.add(1, {"action": access_request.action,
                                          "outcome": "allow" if decision.allowed else "deny"})
    if not decision.allowed:
        raise AppError(Errors.AUTH_FORBIDDEN)
