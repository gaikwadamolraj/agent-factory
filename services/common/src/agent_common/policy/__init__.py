"""IAM-style access policy engine, shared by every service (orchestrator, agent-factory, tool-policy middleware).

Policies are data (`access_policies` rows, edited from the admin UI); the engine is code. Usage:

    from agent_common.policy import (AccessRequest, Subject, InCodePolicyEngine, CachedPolicyLoader,
                                   database_policy_loader)

    loader = CachedPolicyLoader(redis, env, database_policy_loader(db))
    engine = InCodePolicyEngine(loader)
    decision = await engine.authorize(subject, AccessRequest(action="skill.run", resource="skill:ats/screening"))
    if not decision.allowed:
        raise AppError(Errors.AUTH_FORBIDDEN)

Or evaluate a hand-built policy set with no I/O (used by the UI "simulate" feature and tests):

    from agent_common.policy import evaluate
    decision = evaluate(subject, request, policies)

Layers: models (data) -> matcher + conditions -> evaluator (pure) -> loader (versioned cache) -> engine (interface).
An OPA backend can be added later behind PolicyEngine without touching callers (OpaPolicyEngine stub).
"""
from agent_common.policy.conditions import CONDITION_HANDLERS, conditions_hold
from agent_common.policy.engine import InCodePolicyEngine, OpaPolicyEngine, PolicyEngine
from agent_common.policy.evaluator import evaluate
from agent_common.policy.loader import CachedPolicyLoader, PolicyLoader, database_policy_loader
from agent_common.policy.matcher import matches, matches_any
from agent_common.policy.models import (AccessRequest, Effect, PolicyDecision, PolicyDocument, Statement, Subject)

__all__ = [
    "AccessRequest", "CONDITION_HANDLERS", "CachedPolicyLoader", "Effect", "InCodePolicyEngine", "OpaPolicyEngine",
    "PolicyDecision", "PolicyDocument", "PolicyEngine", "PolicyLoader", "Statement", "Subject", "conditions_hold",
    "database_policy_loader", "evaluate", "matches", "matches_any",
]
