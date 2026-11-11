"""Client of the guardrails service, for any service (install `agent-common[mcp]`).

    guard = GuardrailsClient(GuardrailsClientConfig(url="http://agent-guardrail/mcp", pack="ats"),
                             build_service_token_provider(settings))
    result = await guard.check(text, TextSource.RESUME)
    if result.severity is Severity.HIGH: ...        # refuse in code
    elif result.blocked: ...                        # warn the model

`check` never raises: when the service is unavailable or slow it falls back to the local deterministic checks
(`agent_common.prompt_safety` + `agent_common.content_policy`, always available) and says so in `detector`, so a
guardrails outage degrades protection instead of blocking every upload. The fallback reaches the same verdict as
the service minus the classifier, because both go through `agent_common.guardrails.assess`.

Industry rules: `rules` is the merged rule set for the caller's domain pack (base + pack). Without one the base
rules apply, which know nothing about any industry.
"""
from dataclasses import dataclass
from typing import Optional

from agent_common.constants import JWT_AUDIENCE_GUARDRAIL
from agent_common.content_policy import BASE_RULE_SET, PolicyRuleSet
from agent_common.errors import Errors
from agent_common.guardrails.assess import deterministic_signals, is_blocking, reasons_for, severity_for
from agent_common.guardrails.contracts import CheckRequest, CheckResult, TextSource
from agent_common.logging import get_logger, log_event
from agent_common.mcp_client import McpTarget, McpToolClient
from agent_common.resilience import BreakerConfig
from agent_common.security import ServiceTokenProvider

log = get_logger(__name__)
LOCAL_DETECTOR = "patterns-only (guardrails service unavailable)"
MAX_CHECK_CHARS = 200_000


@dataclass(frozen=True)
class GuardrailsClientConfig:
    url: str                        # e.g. http://agent-guardrail/mcp
    timeout_s: float = 20.0         # a long resume is scored in chunks on CPU
    breaker_failures: int = 3
    breaker_reset_s: float = 30.0
    pack: str = ""                  # industry pack id sent with every check ("ats", "insurance")


def local_check(text: str, source: TextSource = TextSource.OTHER,
                rules: PolicyRuleSet = BASE_RULE_SET) -> CheckResult:
    """Deterministic check without the service: injection patterns, hidden encoded layers and the industry's
    request-scope rules. Same verdict as the service minus the classifier score."""
    signals = deterministic_signals(text, source, rules)
    return CheckResult(blocked=is_blocking(signals, classifier_over=False),
                       severity=severity_for(signals, classifier_over=False),
                       signals=signals, reasons=reasons_for(signals, classifier_over=False),
                       detector=LOCAL_DETECTOR)


class LocalGuardrails:
    """Same interface as GuardrailsClient, deterministic checks only (no service configured, tests, local runs)."""

    def __init__(self, rules: PolicyRuleSet = BASE_RULE_SET) -> None:
        self._rules = rules

    async def check(self, text: str, source: TextSource = TextSource.OTHER) -> CheckResult:
        return local_check(text, source, self._rules)


class GuardrailsClient:
    """Guardrails over MCP; `tokens` provides the calling service's tokens (aud=agent-guardrail)."""

    def __init__(self, config: GuardrailsClientConfig, tokens: ServiceTokenProvider,
                 rules: Optional[PolicyRuleSet] = None):
        self._pack = config.pack
        self._rules = rules or BASE_RULE_SET
        self._tools = McpToolClient(McpTarget(
            name="guardrails", url=config.url, audience=JWT_AUDIENCE_GUARDRAIL,
            timeout_s=config.timeout_s, unavailable=Errors.UPSTREAM_UNAVAILABLE, timeout=Errors.UPSTREAM_TIMEOUT,
            breaker=BreakerConfig(config.breaker_failures, config.breaker_reset_s)), tokens)

    async def check(self, text: str, source: TextSource = TextSource.OTHER) -> CheckResult:
        """Should this untrusted text be blocked before it reaches an LLM? Never raises (see module docstring)."""
        request = CheckRequest(text=text[:MAX_CHECK_CHARS], source=source, pack=self._pack)
        try:
            raw = await self._tools.call("check_text", {"request": request.model_dump(mode="json", by_alias=True)})
            return CheckResult.model_validate_json(raw)
        except Exception as e:      # unavailable, timeout, open circuit: degrade to the deterministic checks
            log_event(log, "guardrails.fallback_local", source=source.value, error_type=type(e).__name__)
            return local_check(text, source, self._rules)
