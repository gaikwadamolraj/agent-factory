"""Shared guardrails contract and (with the `mcp` extra) the client: `from agent_common.guardrails.client import ...`."""
from agent_common.guardrails.contracts import CheckRequest, CheckResult, Severity, TextSource

__all__ = ["CheckRequest", "CheckResult", "Severity", "TextSource"]
