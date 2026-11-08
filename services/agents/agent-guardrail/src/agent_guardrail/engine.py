"""Guardrails engine: NeMo Guardrails runs the input rails (rails/*.co + config.yml); the rail's action combines the
deterministic pattern checks (agent_common.prompt_safety) with the injection classifier.

    text -> NeMo input rail "check untrusted text" -> action check_untrusted_text
                                                         patterns: detect(text)          (fake system banners, ...)
                                                         classifier: highest window score (new wordings, languages)
         -> blocked when any pattern fires or the score reaches the threshold

Only input rails run (`options={"rails": ["input"]}`), and no model is configured, so a check never calls an LLM.
New rails (topics, PII) are added as .co flows in the rails directory without touching services.
"""
from pathlib import Path

from agent_common.guardrails.contracts import CheckResult
from agent_common.prompt_safety import detect
from nemoguardrails import LLMRails, RailsConfig

from agent_guardrail.classifier import InjectionClassifier
from agent_guardrail.config import GuardrailsSettings

PACKAGED_RAILS = Path(__file__).with_name("rails")
BLOCKED_MESSAGE = "BLOCKED"
GUARD_VAR = "guard"


class GuardrailsEngine:
    def __init__(self, settings: GuardrailsSettings, classifier: InjectionClassifier):
        self.settings, self.classifier = settings, classifier
        config_dir = settings.rails_config_dir or str(PACKAGED_RAILS)
        self.rails = LLMRails(RailsConfig.from_path(config_dir))
        self.rails.register_action(self.check_untrusted_text, name="check_untrusted_text")

    @property
    def detector(self) -> str:
        return f"nemo-guardrails+{self.classifier.name}@{self.settings.classifier_threshold}"

    async def check_untrusted_text(self, text: str) -> dict:
        """The rail's action: pattern signals + classifier score -> blocked and reasons (a plain dict for Colang)."""
        signals = list(detect(text).kinds)
        score = await self.classifier.score(text)
        over = score.probability >= self.settings.classifier_threshold
        reasons = [f"pattern:{s}" for s in signals] + (["classifier"] if over else [])
        return {"blocked": bool(signals) or over, "score": round(score.probability, 4), "signals": signals,
                "reasons": reasons, "windows": score.windows}

    async def check(self, text: str) -> CheckResult:
        """Run the input rails on the text. The rail's own verdict wins; its action's details fill the result."""
        response = await self.rails.generate_async(messages=[{"role": "user", "content": text}],
                                                   options={"rails": ["input"], "output_vars": [GUARD_VAR]})
        guard = (response.output_data or {}).get(GUARD_VAR) or {}
        content = response.response[-1]["content"] if isinstance(response.response, list) else str(response.response)
        blocked = content.strip() == BLOCKED_MESSAGE or bool(guard.get("blocked"))
        return CheckResult(blocked=blocked, score=guard.get("score", 0.0), signals=guard.get("signals", []),
                           reasons=guard.get("reasons", []) or (["rail"] if blocked else []),
                           detector=self.detector)
