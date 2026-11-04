"""Business metrics of the agent (OTEL -> Prometheus). Labels are low-cardinality only."""
from ats_common.telemetry import get_meter

_meter = get_meter("ats_agent")

screenings_total = _meter.create_counter("ats_agent_screenings_total", description="Screenings by outcome")
step_duration = _meter.create_histogram("ats_agent_step_duration_seconds", unit="s",
                                        description="Duration of each screening step")
steps_total = _meter.create_counter("ats_agent_steps_total", description="Screening steps by who ran them and outcome")
llm_calls_total = _meter.create_counter("ats_llm_calls_total", description="Structured LLM calls by purpose/outcome")
llm_call_duration = _meter.create_histogram("ats_llm_call_duration_seconds", unit="s",
                                            description="Latency of structured LLM calls")
cancellations_total = _meter.create_counter("ats_agent_cancellations_total", description="Cancelled agent requests")
