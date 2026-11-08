"""Agent settings (environment only; secrets from the k8s Secret)."""
from functools import lru_cache

from agent_common.config import McpServiceSettings


class AgentSettings(McpServiceSettings):
    service_name: str = "agent-skill"

    # LLM: gateway, model alias, timeouts and breaker come from agent_common.llm (ConfigMap ats-llm-config)

    # Memory
    memory_ttl_seconds: int = 7 * 24 * 3600
    history_window: int = 12                # messages the model sees per turn
    history_entries: int = 20               # past screenings remembered per user/project

    # Skills. The packaged module under skills/<name>/<version>.py is the fallback; when registry_mcp_url is
    # set, the version activated in the admin UI wins (agent_skill.skills.registry_source).
    default_skill_name: str = "ats_screening"
    default_skill_version: str = "v1"
    skill_domain: str = "ats"               # the registry skill's domain; with the name it is the natural key
    registry_mcp_url: str = ""              # e.g. http://registry-service; empty = packaged skills only
    registry_timeout_s: float = 10.0
    registry_cache_entries: int = 256

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
