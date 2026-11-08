"""OpenTelemetry metrics for registry-service (low-cardinality labels only, CLAUDE.md section 9)."""
from agent_common.telemetry import get_meter

_meter = get_meter("registry_service")

# One counter for every tool invocation, labelled by tool name, front door (mcp|api) and outcome (ok|denied|error).
tool_calls_total = _meter.create_counter(
    "agent_registry_tool_calls_total", description="registry-service tool invocations by tool, front door and outcome")

# Authorization decisions (allow|deny) by action, so a spike in denials is visible.
authz_decisions_total = _meter.create_counter(
    "agent_registry_authz_decisions_total", description="registry-service policy decisions by action and outcome")

# Calls delegated to the auth-service (create user, invite, set role, deactivate), by operation and outcome.
identity_calls_total = _meter.create_counter(
    "agent_registry_identity_calls_total", description="Identity operations delegated to the auth-service by op/outcome")
