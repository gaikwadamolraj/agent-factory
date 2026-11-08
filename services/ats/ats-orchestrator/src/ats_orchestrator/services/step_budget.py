"""Step budget of one user request, shared by every agent hop (assistant LLM calls, tool calls, code-author calls,
code runs). It guarantees the multi-agent loop ends: when the budget is spent, tools stop and the assistant answers."""
from dataclasses import dataclass, field

from agent_common.logging import get_logger, log_event

log = get_logger(__name__)


@dataclass
class StepBudget:
    limit: int
    used: int = 0
    steps: list[str] = field(default_factory=list)
    _logged: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self, step: str) -> bool:
        """Spend one step; False when the budget is spent (the caller must stop and answer with what it has)."""
        if self.used >= self.limit:
            if not self._logged:
                log_event(log, "turn.step_limit_reached", limit=self.limit, steps="|".join(self.steps))
                self._logged = True
            return False
        self.used += 1
        self.steps.append(step)
        return True

    def force(self, step: str) -> None:
        """Record a step that must happen anyway (the final answer), so it still shows in the trace."""
        self.used += 1
        self.steps.append(step)
