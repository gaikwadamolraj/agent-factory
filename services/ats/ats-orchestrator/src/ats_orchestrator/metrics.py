"""Business metrics (OTEL -> Prometheus -> Grafana). Labels are low-cardinality ids/enums only; per-user detail
lives in the logs (Loki), which carry user_id and username on every record."""
import time
from collections.abc import Callable
from threading import Lock

from opentelemetry.metrics import CallbackOptions, Observation

from agent_common.telemetry import get_meter

_meter = get_meter("ats_orchestrator")

turns_total = _meter.create_counter("agent_turns_total", description="Chat turns by action and outcome")
turn_duration = _meter.create_histogram("agent_turn_duration_seconds", unit="s", description="Chat turn duration")
active_turns = _meter.create_up_down_counter("agent_active_turns", description="Turns currently running")
screenings_total = _meter.create_counter("agent_screenings_total", description="Screenings by recommendation")
screening_score = _meter.create_histogram("agent_screening_score", description="Screening scores (0-100)")
aborts_total = _meter.create_counter("agent_turn_aborts_total", description="Turns aborted by users")
agent_calls_total = _meter.create_counter("agent_calls_total", description="MCP calls to the agent by tool/outcome")
injection_flags = _meter.create_counter("agent_prompt_injection_detected_total",
                                       description="Untrusted text that tried to instruct an LLM, by source")
score_corrections = _meter.create_counter("agent_score_claims_corrected_total",
                                         description="Chat replies whose score did not match the stored screening")


class ActiveUsers:
    """Distinct users seen per client within a time window (exported as gauge agent_active_users)."""

    def __init__(self, window_s: int):
        self.window_s = window_s
        self._seen: dict[int, tuple[int, float]] = {}
        self._lock = Lock()

    def touch(self, user_id: int, client_id: int) -> None:
        with self._lock:
            self._seen[user_id] = (client_id, time.monotonic())

    def counts(self) -> dict[int, int]:
        cutoff = time.monotonic() - self.window_s
        with self._lock:
            self._seen = {u: v for u, v in self._seen.items() if v[1] >= cutoff}
            result: dict[int, int] = {}
            for client_id, _ in self._seen.values():
                result[client_id] = result.get(client_id, 0) + 1
        return result


def register_gauges(active_users: ActiveUsers, queue_waiting: Callable[[], int]) -> None:
    def _users(_: CallbackOptions):
        return [Observation(n, {"client_id": str(c)}) for c, n in active_users.counts().items()] or [Observation(0)]

    def _waiting(_: CallbackOptions):
        return [Observation(queue_waiting())]

    _meter.create_observable_gauge("agent_active_users", callbacks=[_users], description="Users active in the window")
    _meter.create_observable_gauge("agent_llm_queue_waiting", callbacks=[_waiting], description="Turns waiting in queue")
