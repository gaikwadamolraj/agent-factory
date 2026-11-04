"""Agent settings (environment only; secrets from the k8s Secret)."""
from functools import lru_cache
from typing import Optional

from ats_common.config import McpServiceSettings


class AgentSettings(McpServiceSettings):
    service_name: str = "ats-agent"

    # LLM (LM Studio / any OpenAI-compatible server)
    llm_endpoint: str
    llm_api_key: str = "not-needed"
    model_name: str = "meta-llama-3.1-8b-instruct"
    llm_max_tokens: Optional[int] = None
    llm_timeout_s: float = 300.0            # one structured call
    llm_retry_attempts: int = 2             # bounded retries for transient failures
    llm_breaker_failures: int = 5           # consecutive failures that open the LLM circuit
    llm_breaker_reset_s: float = 30.0

    # Memory
    memory_ttl_seconds: int = 7 * 24 * 3600
    history_window: int = 12                # messages the model sees per turn
    history_entries: int = 20               # past screenings remembered per user/project

    # Skills
    default_skill_name: str = "ats_screening"
    default_skill_version: str = "v1"

    # Input limits
    max_jd_chars: int = 50_000
    max_resume_chars: int = 200_000
    max_question_chars: int = 2_000

    # Zero trust: which tools each caller may use (allowed_callers, host, port come from McpServiceSettings)
    tool_policy: str = "ats-orchestrator=screen_candidate|ask_agent|cancel_request"
    port: int = 8001


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
