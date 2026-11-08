"""LLM metrics for every service (one definition). Labels are low-cardinality: purpose, op, outcome."""
from agent_common.telemetry import get_meter

_meter = get_meter("agent_llm")

llm_calls_total = _meter.create_counter("agent_llm_calls_total", description="LLM calls by purpose, operation, outcome")
llm_call_duration = _meter.create_histogram("agent_llm_call_duration_seconds", unit="s",
                                            description="Latency of LLM calls by purpose")
