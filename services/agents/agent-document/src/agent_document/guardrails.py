"""Build the guardrails client the document agent uses to injection-scan extracted text.

Reuses the shared `agent_common.guardrails` client, so a guardrails outage degrades to local pattern checks instead of
failing the read (the client never raises). When no guardrail URL is configured we use the local-only checks directly.
"""
from agent_common.guardrails.client import GuardrailsClient, GuardrailsClientConfig, LocalGuardrails
from agent_common.security import ServiceTokenProvider

from agent_document.config import DocumentSettings

Guardrails = GuardrailsClient | LocalGuardrails


def build_guardrails(settings: DocumentSettings, tokens: ServiceTokenProvider) -> Guardrails:
    """agent-guardrail client when a URL is configured, otherwise local pattern checks only."""
    if not settings.guardrail_mcp_url:
        return LocalGuardrails()
    config = GuardrailsClientConfig(
        url=settings.guardrail_mcp_url,
        timeout_s=settings.guardrail_timeout_s,
        breaker_failures=settings.guardrail_breaker_failures,
        breaker_reset_s=settings.guardrail_breaker_reset_s,
    )
    return GuardrailsClient(config, tokens)
