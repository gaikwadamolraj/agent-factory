"""Wire contract of the guardrails service (MCP tool `check_text`), camelCase on the wire.

Every service that lets untrusted text reach an LLM (uploads, chat messages, documents from other sources) asks the
guardrails service first; the result says whether to block and why. One definition, imported by both sides.
"""
from enum import StrEnum

from pydantic import Field

from agent_common.models import ApiModel, ApiResponse

MAX_CHECK_CHARS = 200_000


class TextSource(StrEnum):
    """Where the text comes from (a metric label, so a fixed list)."""
    CHAT_MESSAGE = "chat_message"
    RESUME = "resume"
    JOB_DESCRIPTION = "job_description"
    DOCUMENT = "document"
    OTHER = "other"


class Severity(StrEnum):
    """How sure the guardrail is, which is what tells a caller whether to refuse or only warn the model.

    HIGH - refuse in code: the classifier is over its threshold, or a signal that only an attack produces fired
           (an override order, an encoded payload, a harmful or out-of-journey request).
    LOW  - let the text through with a note to the model (a lone ambiguous signal, e.g. banner-like punctuation).
    """
    NONE = "none"
    LOW = "low"
    HIGH = "high"


class CheckRequest(ApiModel):
    text: str = Field(min_length=1, max_length=MAX_CHECK_CHARS)
    source: TextSource = TextSource.OTHER
    pack: str = Field(default="", max_length=64)    # industry pack id ("ats", "insurance"): selects the scope rules


class CheckResult(ApiResponse):
    blocked: bool                                   # True = do not let this text reach an LLM
    severity: Severity = Severity.NONE              # NONE/LOW/HIGH (see Severity): HIGH means refuse in code
    score: float = 0.0                              # classifier probability of injection/jailbreak (max over chunks)
    signals: list[str] = Field(default_factory=list)  # deterministic kinds (prompt_safety + content_policy)
    reasons: list[str] = Field(default_factory=list)  # short, user-safe reasons ("classifier", "pattern:override",
                                                      # "policy:harmful_request")
    detector: str = ""                              # e.g. "nemo-guardrails+prompt-guard-2-86m@0.5"
