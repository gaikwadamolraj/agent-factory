"""Client of the guardrails service, for any service (install `agent-common[mcp]`).

    guard = GuardrailsClient(GuardrailsClientConfig(url="http://agent-guardrail/mcp"),
                             build_service_token_provider(settings))
    result = await guard.check(text, TextSource.RESUME)
    if result.blocked: ...

`check` never raises: when the service is unavailable or slow it falls back to the local pattern checks
(`agent_common.prompt_safety`, always available) and says so in `detector`, so a guardrails outage degrades protection
instead of blocking every upload. Callers decide what a block means for them (reject an upload, warn the model).
"""
from dataclasses import dataclass

from agent_common.constants import JWT_AUDIENCE_GUARDRAIL
from agent_common.errors import Errors
from agent_common.guardrails.contracts import CheckRequest, CheckResult, TextSource
from agent_common.logging import get_logger, log_event
from agent_common.mcp_client import McpTarget, McpToolClient
from agent_common.prompt_safety import detect
from agent_common.resilience import BreakerConfig
from agent_common.security import ServiceTokenProvider

log = get_logger(__name__)
LOCAL_DETECTOR = "patterns-only (guardrails service unavailable)"


@dataclass(frozen=True)
class GuardrailsClientConfig:
    url: str                        # e.g. http://agent-guardrail/mcp
    timeout_s: float = 20.0         # a long resume is scored in chunks on CPU
    breaker_failures: int = 3
    breaker_reset_s: float = 30.0


def local_check(text: str) -> CheckResult:
    """Pattern-only check (no service): blocks on any prompt_safety signal."""
    report = detect(text)
    return CheckResult(blocked=report.suspicious, signals=list(report.kinds),
                       reasons=[f"pattern:{kind}" for kind in report.kinds], detector=LOCAL_DETECTOR)


class LocalGuardrails:
    """Same interface as GuardrailsClient, pattern checks only (no service configured, tests, local runs)."""

    async def check(self, text: str, source: TextSource = TextSource.OTHER) -> CheckResult:
        return local_check(text)


class GuardrailsClient:
    """Guardrails over MCP; `tokens` provides the calling service's tokens (aud=agent-guardrail)."""

    def __init__(self, config: GuardrailsClientConfig, tokens: ServiceTokenProvider):
        self._tools = McpToolClient(McpTarget(
            name="guardrails", url=config.url, audience=JWT_AUDIENCE_GUARDRAIL,
            timeout_s=config.timeout_s, unavailable=Errors.UPSTREAM_UNAVAILABLE, timeout=Errors.UPSTREAM_TIMEOUT,
            breaker=BreakerConfig(config.breaker_failures, config.breaker_reset_s)), tokens)

    async def check(self, text: str, source: TextSource = TextSource.OTHER) -> CheckResult:
        """Should this untrusted text be blocked before it reaches an LLM? Never raises (see module docstring)."""
        request = CheckRequest(text=text[:200_000], source=source)
        try:
            raw = await self._tools.call("check_text", {"request": request.model_dump(mode="json", by_alias=True)})
            return CheckResult.model_validate_json(raw)
        except Exception as e:      # unavailable, timeout, open circuit: degrade to the local patterns
            log_event(log, "guardrails.fallback_local", source=source.value, error_type=type(e).__name__)
            return local_check(text)
