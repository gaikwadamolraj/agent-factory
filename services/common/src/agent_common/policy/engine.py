"""The policy engine: the single interface every caller uses to ask "may this subject do this?".

`PolicyEngine` is the seam. `InCodePolicyEngine` is today's implementation - it loads a subject's effective policies
(through a loader) and runs the pure evaluator. `OpaPolicyEngine` is the documented future seam: the same interface,
backed by Open Policy Agent, swapped in by configuration without touching any caller (docs/impl_agent_factory.md
section 4, "An OPA backend can be added later behind the same interface").
"""
from typing import Protocol, runtime_checkable

from agent_common.logging import get_logger, log_event
from agent_common.policy.evaluator import evaluate
from agent_common.policy.loader import CachedPolicyLoader, PolicyLoader
from agent_common.policy.models import AccessRequest, PolicyDecision, Subject

log = get_logger(__name__)


@runtime_checkable
class PolicyEngine(Protocol):
    """Decides access for a subject. Implementations must be safe to share across concurrent requests."""

    async def authorize(self, subject: Subject, request: AccessRequest) -> PolicyDecision:
        """Return the decision for `subject` performing `request` (never raises on a plain deny)."""
        ...


class InCodePolicyEngine:
    """In-code engine: load the subject's effective policies, then run the pure evaluator.

    Give it any object with an async `load(subject) -> list[PolicyDocument]` method (CachedPolicyLoader) or a plain
    PolicyLoader callable. The decision itself is computed by agent_common.policy.evaluator.evaluate.
    """

    def __init__(self, loader: "CachedPolicyLoader | PolicyLoader"):
        # Accept either the cached loader (has .load) or a bare callable, so tests and callers can pass the simpler one.
        self._load = loader.load if isinstance(loader, CachedPolicyLoader) else loader

    async def authorize(self, subject: Subject, request: AccessRequest) -> PolicyDecision:
        """Load the subject's effective policies and evaluate the request against them."""
        policies = await self._load(subject)
        decision = evaluate(subject, request, policies)
        log_event(log, "policy.decision", user_id=subject.user_id, action=request.action,
                  resource=request.resource, allowed=decision.allowed, effect=decision.effect)
        return decision


class OpaPolicyEngine:
    """Future seam: evaluate the same AccessRequest against an Open Policy Agent sidecar/service.

    Not implemented yet (user decision: in-code engine now). When added, it takes the OPA endpoint from settings,
    sends the subject + request as input, and maps OPA's result onto PolicyDecision - so callers keep using
    PolicyEngine.authorize unchanged.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "OpaPolicyEngine is a future backend; use InCodePolicyEngine (docs/impl_agent_factory.md section 4).")

    async def authorize(self, subject: Subject, request: AccessRequest) -> PolicyDecision:  # pragma: no cover
        raise NotImplementedError
