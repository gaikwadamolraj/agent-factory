"""IAM access-policy administration and the 'simulate' check (docs/impl_registry_service.md section 4).

Policies are data: these tools CRUD `access_policies` rows and bump the global policy version on every change so every
replica recompiles. A document is validated against agent_common.policy.PolicyDocument before it is stored. An
escalation guardrail stops a non-super-admin from granting the full-wildcard (super-admin) bundle.
"""
from agent_common.authz import bump_policy_version
from agent_common.db import AccessPolicyRepository
from agent_common.db.platform_tables import AccessPolicy
from agent_common.errors import AppError
from agent_common.policy import AccessRequest, Subject, evaluate
from agent_common.policy.models import Effect, PolicyDocument

from registry_service import audit
from registry_service.authorization import ToolContext
from registry_service.constants import SUPER_ADMIN_ACTION, SUPER_ADMIN_RESOURCE
from registry_service.errors import FactoryErrors
from registry_service.operations._common import get_by_external_id


def _policy_dict(policy: AccessPolicy) -> dict:
    return {"id": str(policy.external_id), "principalType": policy.principal_type, "principalId": policy.principal_id,
            "name": policy.name, "document": policy.document, "enabled": policy.enabled}


def _validate_document(raw: dict) -> PolicyDocument:
    """Parse + validate the policy document (raises a pydantic error -> VAL-INV-0001 at the edge)."""
    return PolicyDocument.model_validate(raw)


def _is_super_bundle(document: PolicyDocument) -> bool:
    """True when the document grants the full-wildcard super-admin bundle."""
    return any(s.effect == Effect.ALLOW and SUPER_ADMIN_ACTION in s.actions and SUPER_ADMIN_RESOURCE in s.resources
               and not s.conditions for s in document.statements)


def _guard_escalation(ctx: ToolContext, document: PolicyDocument) -> None:
    """Nobody grants access broader than their own: only a super admin may create the super-admin bundle."""
    if _is_super_bundle(document) and not (ctx.subject and ctx.subject.is_super_admin):
        raise AppError(FactoryErrors.ESCALATION_FORBIDDEN)


async def list_policies(ctx: ToolContext, req) -> dict:
    """Policies of the client, optionally filtered to one principal."""
    repo = AccessPolicyRepository(ctx.session)
    if req.principal_type and req.principal_id:
        rows = await repo.list_for_principal(ctx.client_id, req.principal_type, req.principal_id)
    else:
        from sqlmodel import col, select

        rows = (await ctx.session.exec(select(AccessPolicy).where(
            AccessPolicy.client_id == ctx.client_id, col(AccessPolicy.is_deleted).is_(False)))).all()
    return {"items": [_policy_dict(p) for p in rows], "total": len(rows)}


async def create_policy(ctx: ToolContext, req) -> dict:
    """Create an access policy for a principal in the caller's client; bumps the policy version."""
    document = _validate_document(req.document)
    _guard_escalation(ctx, document)
    policy = AccessPolicy(client_id=ctx.client_id, principal_type=req.principal_type, principal_id=req.principal_id,
                          name=req.name, document=document.model_dump(mode="json"), enabled=req.enabled)
    audit.stamp_created(policy, ctx.actor_id)
    await AccessPolicyRepository(ctx.session).add(policy)
    await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("create_policy", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(policy.external_id),
                 after={"principalType": policy.principal_type, "principalId": policy.principal_id})
    return _policy_dict(policy)


async def update_policy(ctx: ToolContext, req) -> dict:
    """Edit a policy's document/name/enabled; bumps the policy version."""
    policy = await get_by_external_id(ctx.session, AccessPolicy, req.policy_id, ctx.client_id)
    if policy is None:
        raise AppError(FactoryErrors.POLICY_NOT_FOUND)
    before = {"enabled": policy.enabled}
    if req.document is not None:
        document = _validate_document(req.document)
        _guard_escalation(ctx, document)
        policy.document = document.model_dump(mode="json")
    if req.name is not None:
        policy.name = req.name
    if req.enabled is not None:
        policy.enabled = req.enabled
    audit.stamp_updated(policy, ctx.actor_id)
    ctx.session.add(policy)
    await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("update_policy", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(policy.external_id),
                 before=before, after={"enabled": policy.enabled})
    return _policy_dict(policy)


async def delete_policy(ctx: ToolContext, req) -> dict:
    """Soft-delete a policy; bumps the policy version."""
    policy = await get_by_external_id(ctx.session, AccessPolicy, req.policy_id, ctx.client_id)
    if policy is None:
        raise AppError(FactoryErrors.POLICY_NOT_FOUND)
    audit.stamp_deleted(policy, ctx.actor_id, reason="deleted via admin")
    ctx.session.add(policy)
    await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("delete_policy", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(policy.external_id))
    return {"id": str(policy.external_id), "deleted": True}


async def simulate_policy(ctx: ToolContext, req) -> dict:
    """"Can user X do action Y on resource Z?" for the admin UI: evaluate the target user's effective policies.

    Builds the target user's Subject and effective policies, then runs the pure evaluator and returns the full
    decision (effect + the statement that decided), so the UI can explain it.
    """
    from registry_service.subjects import build_subject

    subject: Subject = await build_subject(ctx.session, req.user_id, ctx.client_id)
    policies = await AccessPolicyRepository(ctx.session).effective_for(subject)
    request = AccessRequest(action=req.action, resource=req.resource, resource_client_id=req.resource_client_id,
                            resource_project_id=req.resource_project_id, resource_author_id=req.resource_author_id)
    decision = evaluate(subject, request, policies)
    return {"userId": req.user_id, "action": req.action, "resource": req.resource, "allowed": decision.allowed,
            "effect": decision.effect, "reason": decision.reason, "matchedConditions": decision.matched_conditions}
