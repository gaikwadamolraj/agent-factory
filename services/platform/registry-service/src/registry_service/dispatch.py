"""The shared auth+policy+dispatch pipeline - the one authority both front doors call (docs/impl_registry_service.md
section 3). The MCP tool wrappers and the REST wrapper both reduce a request to (principal, tool_name, raw_args) and
hand it here, so authentication has already happened upstream and authorization + validation + tenant scoping +
dispatch + audit happen in exactly one place.

Steps for every tool call:
 1. look up the ToolSpec (unknown tool -> default deny / 404);
 2. validate the raw arguments against the tool's request model (extra=forbid, bounds) -> VAL-INV-0001 on failure;
 3. resolve the effective client (a super admin may act in another client via X-Client-Id; everyone else is pinned
    to their own client) and build the policy Subject from the user's OWN client;
 4. authorize (agent_common.policy, default deny; machine TOOL_POLICY for service callers);
 5. open a tenant-scoped session (RLS GUC set from the verified client) and run the handler;
 6. the handler audits its own before/after; the unit of work commits on success.
"""
from typing import Any, Optional

from pydantic import ValidationError

from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger, log_event
from agent_common.policy import AccessRequest

from registry_service import catalog, metrics
from registry_service.authorization import ToolContext, authorize
from registry_service.errors import FactoryErrors
from registry_service.principals import ServicePrincipal, UserPrincipal
from registry_service.subjects import build_subject

log = get_logger(__name__)


def _validation_fields(error: ValidationError) -> list[dict]:
    """Turn a pydantic error into the error envelope's `fields` list (field + issue)."""
    return [{"field": ".".join(str(p) for p in e.get("loc", ())) or "body", "issue": e.get("msg", "invalid")}
            for e in error.errors()]


def _service_client_id(raw_args: dict[str, Any]) -> int:
    """The client a service call acts in (service tokens carry no client; the argument must)."""
    value = raw_args.get("clientId", raw_args.get("client_id"))
    if not isinstance(value, int) or value < 1:
        raise AppError(Errors.AUTH_FORBIDDEN, detail="A service call must specify clientId.")
    return value


async def invoke_tool(container, principal, tool_name: str, raw_args: dict[str, Any],
                      client_header: Optional[int] = None, front_door: str = "api") -> dict:
    """Authorize and run a registered tool. Returns the handler's JSON-ready dict; raises AppError on any refusal."""
    spec = catalog.get_spec(tool_name)
    if spec is None:
        metrics.tool_calls_total.add(1, {"tool": tool_name, "front_door": front_door, "outcome": "unknown"})
        raise AppError(FactoryErrors.UNKNOWN_TOOL)                       # unmapped tool: nothing is implicit
    try:
        request = spec.request_model.model_validate(raw_args)
    except ValidationError as e:
        raise AppError(Errors.VALIDATION_FAILED, fields=_validation_fields(e)) from None

    try:
        result = await _run(container, principal, spec, request, raw_args, client_header)
        metrics.tool_calls_total.add(1, {"tool": tool_name, "front_door": front_door, "outcome": "ok"})
        return result
    except AppError as e:
        outcome = "denied" if e.spec.status in (401, 403) else "error"
        metrics.tool_calls_total.add(1, {"tool": tool_name, "front_door": front_door, "outcome": outcome})
        raise


async def _run(container, principal, spec, request, raw_args, client_header) -> dict:
    """Resolve the tenant context + subject, authorize, then run the handler in a tenant-scoped unit of work."""
    if isinstance(principal, ServicePrincipal):
        effective_client = _service_client_id(raw_args)
        async with container.db.tenant_session(effective_client) as session:
            ctx = ToolContext(principal, None, effective_client, session, container, None)
            await authorize(ctx, spec, request)
            return await spec.handler(ctx, request)

    # User principal.
    own_client = principal.claims.client_id
    effective_client = client_header or own_client
    cross_tenant = effective_client != own_client

    if not cross_tenant:
        # Common path: one session; the subject is built in the same (own-client) session.
        async with container.db.tenant_session(own_client) as session:
            subject = await build_subject(session, principal.claims.user_id, own_client)
            ctx = ToolContext(principal, principal.claims.user_id, own_client, session, container, subject)
            await authorize(ctx, spec, request)
            return await spec.handler(ctx, request)

    # Cross-tenant: only a super admin may act in another client (audited). The subject keeps the user's OWN client,
    # so the policy engine evaluates the super admin's own (wildcard) policies; the operation runs under the target.
    async with container.db.tenant_session(own_client) as subject_session:
        subject = await build_subject(subject_session, principal.claims.user_id, own_client)
    if not subject.is_super_admin:
        log_event(log, "factory.cross_client_denied", user_id=principal.claims.user_id,
                  requested_client=effective_client)
        raise AppError(FactoryErrors.CROSS_CLIENT_FORBIDDEN)
    async with container.db.tenant_session(effective_client) as session:
        ctx = ToolContext(UserPrincipal(principal.claims, assume_client_id=effective_client),
                          principal.claims.user_id, effective_client, session, container, subject)
        await authorize(ctx, spec, request)
        return await spec.handler(ctx, request)


async def invoke_remote(container, principal, server: str, tool: str, raw_args: dict[str, Any],
                        client_header: Optional[int] = None) -> dict:
    """Catalogue proxy: authorize (action `tool:{server}.{tool}`) then invoke the tool on a registered MCP server.

    Uses the same pipeline shape as a native tool, so the human IAM layer (users) and the machine layer (services)
    both gate a proxied call, and the result is returned verbatim as text.
    """
    action, resource = f"tool:{server}.{tool}", f"tool:{server}/{tool}"
    if isinstance(principal, ServicePrincipal):
        # Machine layer: the service must be allow-listed for a synthetic "mcp:{server}.{tool}" tool name.
        allowed = container.tool_policy.get(principal.caller, set())
        if "*" not in allowed and f"mcp:{server}.{tool}" not in allowed:
            raise AppError(Errors.SERVICE_FORBIDDEN)
        effective_client = _service_client_id(raw_args)
    else:
        own_client = principal.claims.client_id
        effective_client = client_header or own_client
        async with container.db.tenant_session(own_client) as session:
            subject = await build_subject(session, principal.claims.user_id, own_client)
        if effective_client != own_client and not subject.is_super_admin:
            raise AppError(FactoryErrors.CROSS_CLIENT_FORBIDDEN)
        decision = await container.policy_engine.authorize(
            subject, AccessRequest(action=action, resource=resource, resource_client_id=effective_client))
        metrics.authz_decisions_total.add(1, {"action": action, "outcome": "allow" if decision.allowed else "deny"})
        if not decision.allowed:
            raise AppError(Errors.AUTH_FORBIDDEN)

    text = await container.catalogue.invoke(server, tool, raw_args.get("arguments", {}))
    return {"server": server, "tool": tool, "result": text}
