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


class CheckRequest(ApiModel):
    text: str = Field(min_length=1, max_length=MAX_CHECK_CHARS)
    source: TextSource = TextSource.OTHER


class CheckResult(ApiResponse):
    blocked: bool                                   # True = do not let this text reach an LLM
    score: float = 0.0                              # classifier probability of injection/jailbreak (max over chunks)
    signals: list[str] = Field(default_factory=list)  # pattern signals (agent_common.prompt_safety kinds)
    reasons: list[str] = Field(default_factory=list)  # short, user-safe reasons ("classifier", "pattern:override")
    detector: str = ""                              # e.g. "nemo-guardrails+prompt-guard-2-86m@0.5"
