"""Business metrics of the agent (OTEL -> Prometheus). Labels are low-cardinality only."""
from ats_common.telemetry import get_meter

_meter = get_meter("ats_agent")

screenings_total = _meter.create_counter("ats_agent_screenings_total", description="Screenings by outcome")
step_duration = _meter.create_histogram("ats_agent_step_duration_seconds", unit="s",
                                        description="Duration of each screening step")
steps_total = _meter.create_counter("ats_agent_steps_total", description="Screening steps by who ran them and outcome")
cancellations_total = _meter.create_counter("ats_agent_cancellations_total", description="Cancelled agent requests")
injection_flags = _meter.create_counter("ats_prompt_injection_detected_total",
                                     description="Untrusted text that tried to instruct an LLM, by source")
