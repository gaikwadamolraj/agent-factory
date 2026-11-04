"""Agent settings (environment only; secrets from the k8s Secret)."""
from functools import lru_cache

from ats_common.config import McpServiceSettings


class AgentSettings(McpServiceSettings):
    service_name: str = "ats-agent"

    # LLM: gateway, model alias, timeouts and breaker come from ats_common.llm (ConfigMap ats-llm-config)

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
