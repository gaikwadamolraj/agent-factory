"""Guardrails agent settings (environment only; the Hugging Face token comes from the k8s Secret)."""
from functools import lru_cache
from typing import Optional

from agent_common.config import McpServiceSettings
from pydantic import SecretStr


class GuardrailsSettings(McpServiceSettings):
    service_name: str = "agent-guardrail"
    port: int = 8003

    # Injection / jailbreak classifier (Hugging Face). Prompt Guard 2 needs the Llama licence accepted on the
    # model page for the token's account; until then an open model with the same purpose is used.
    classifier_model: str = "protectai/deberta-v3-base-prompt-injection-v2"
    classifier_malicious_labels: str = "INJECTION,MALICIOUS,JAILBREAK,LABEL_1"   # matched against id2label
    classifier_threshold: float = 0.5        # block when the injection probability of any chunk reaches this
    classifier_chunk_tokens: int = 512       # long texts (resumes) are scored in windows of this many tokens
    classifier_chunk_stride: int = 64        # overlap between windows, so an order cut in two is still seen
    classifier_max_chunks: int = 64          # bound per request (200k chars is ~50 windows)
    classifier_threads: int = 2              # torch CPU threads
    classifier_concurrency: int = 2          # requests scored at the same time (CPU bound)
    classifier_max_decoded_layers: int = 4   # hidden (base64/hex/...) layers also scored, per text
    hf_token: Optional[SecretStr] = None     # env HF_TOKEN; only needed for gated models
    model_cache_dir: str = "/models"         # where the model is downloaded (a volume in k8s)

    # NeMo Guardrails configuration (input rails; no LLM call per check)
    rails_config_dir: str = ""               # empty = the packaged agent_guardrail/rails directory

    # Request-scope rules per industry (agent_common.content_policy). The rules are data carried by the domain
    # pack: `packs/<pack>/<version>/guardrails/policy.json`. An empty directory = base rules only, which know
    # nothing about any industry.
    packs_dir: str = "/packs"
    policy_rules_cache_entries: int = 32     # industries cached in memory (bounded)
    # Verdict policy, overridable without a release. Empty = the defaults of agent_common.guardrails.assess.
    high_signals: str = ""                   # signals that make a verdict HIGH (a refusal in code)
    chat_only_signals: str = ""              # signals judged only for text a person typed

    max_text_chars: int = 200_000

    # Zero trust: who may call which tool (allowed_callers / tool_policy from McpServiceSettings)
    allowed_callers: str = "ats-orchestrator,agent-knowledge-graph"
    tool_policy: str = "ats-orchestrator=check_text,agent-knowledge-graph=check_text"

    @property
    def malicious_labels(self) -> set[str]:
        return {label.strip().upper() for label in self.classifier_malicious_labels.split(",") if label.strip()}

    @property
    def high_signal_set(self) -> frozenset[str]:
        """Signals that make a verdict HIGH; the shared defaults when the setting is empty."""
        return _signal_set(self.high_signals, DEFAULT_HIGH_SIGNALS)

    @property
    def chat_only_signal_set(self) -> frozenset[str]:
        """Signals judged only for chat messages; the shared defaults when the setting is empty."""
        return _signal_set(self.chat_only_signals, DEFAULT_CHAT_ONLY_SIGNALS)


def _signal_set(configured: str, default: frozenset[str]) -> frozenset[str]:
    names = {name.strip() for name in (configured or "").split(",") if name.strip()}
    return frozenset(names) if names else default


@lru_cache
def get_settings() -> GuardrailsSettings:
    return GuardrailsSettings()
