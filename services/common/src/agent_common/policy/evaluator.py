"""The pure policy evaluator: given a subject's effective policies and one access request, decide allow or deny.

This function is framework-free and side-effect-free - no Redis, no database, no I/O. Loading and caching a subject's
policies lives in agent_common.policy.loader; wiring it into a service lives in agent_common.policy.engine. Keeping the
decision pure makes it trivial to reason about and to reuse from the orchestrator, agent-factory and the tool-policy
middleware.

IAM semantics (docs/impl_agent_factory.md section 4):
1. gather every statement from every effective policy;
2. an explicit `deny` that matches wins immediately over any allow;
3. otherwise an `allow` that matches grants access;
4. otherwise access is denied by default (default deny).
"""
from agent_common.policy.conditions import conditions_hold, enabled_condition_names
from agent_common.policy.matcher import matches_any
from agent_common.policy.models import AccessRequest, Effect, PolicyDecision, PolicyDocument, Statement, Subject


def _statement_matches(statement: Statement, subject: Subject, request: AccessRequest) -> bool:
    """True when the statement's action, resource and all enabled conditions match the request."""
    return (matches_any(statement.actions, request.action)
            and matches_any(statement.resources, request.resource)
            and conditions_hold(statement.conditions, subject, request))


def evaluate(subject: Subject, request: AccessRequest, policies: list[PolicyDocument]) -> PolicyDecision:
    """Decide whether `subject` may perform `request` given its effective `policies`.

    Returns a PolicyDecision naming the statement that decided (for the admin 'simulate' view). An explicit matching
    deny wins over any allow; with no deny, the first matching allow grants; with neither, access is denied by default.
    """
    first_allow: tuple[int, int, Statement] | None = None   # (policy_index, statement_index, statement)

    # Scan every statement: a matching deny ends the search at once; remember the first matching allow meanwhile.
    for policy_index, policy in enumerate(policies):
        for statement_index, statement in enumerate(policy.statements):
            if not _statement_matches(statement, subject, request):
                continue
            if statement.effect == Effect.DENY:
                return PolicyDecision(
                    allowed=False, effect="deny",
                    reason="An explicit deny statement matched the request.",
                    policy_index=policy_index, statement_index=statement_index,
                    matched_conditions=enabled_condition_names(statement.conditions))
            if first_allow is None:
                first_allow = (policy_index, statement_index, statement)

    if first_allow is not None:
        policy_index, statement_index, statement = first_allow
        return PolicyDecision(
            allowed=True, effect="allow",
            reason="An allow statement matched and no deny overrode it.",
            policy_index=policy_index, statement_index=statement_index,
            matched_conditions=enabled_condition_names(statement.conditions))

    return PolicyDecision(allowed=False, effect="default",
                          reason="No statement matched the request; access is denied by default.")
