"""LLM metrics for every service (one definition). Labels are low-cardinality: purpose, op, outcome."""
from ats_common.telemetry import get_meter

_meter = get_meter("ats_llm")

llm_calls_total = _meter.create_counter("ats_llm_calls_total", description="LLM calls by purpose, operation, outcome")
llm_call_duration = _meter.create_histogram("ats_llm_call_duration_seconds", unit="s",
                                            description="Latency of LLM calls by purpose")
