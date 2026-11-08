"""Which model alias a call uses: purpose + (optional) client policy -> gateway alias.

    model_for(Purpose.CHAT)                  -> "ats-chat"
    model_for(Purpose.CHAT, client_id=2)     -> "ats-chat-quality"   (client 2 has profile "high-quality")
    model_identity(Purpose.SCREENING, 1)     -> "ats-screening@3"     (alias + LLM_CONFIG_VERSION, for cache keys)

Embeddings are never per client: every vector of the platform must come from the same model, or search breaks.
"""
from enum import StrEnum
from typing import Optional

from agent_common.llm.settings import LLMSettings, get_llm_settings


class Purpose(StrEnum):
    CHAT = "chat"              # conversational assistant (tool calling)
    SCREENING = "screening"    # screening agent (structured steps)
    EXTRACTION = "extraction"  # knowledge graph extraction
    ANSWER = "answer"          # grounded answers from retrieved facts
    EMBEDDING = "embedding"    # vectors (documents, graph, cache)


def profile_of(client_id: Optional[int], settings: Optional[LLMSettings] = None) -> str:
    """The client's model profile name ("default" when it has none)."""
    settings = settings or get_llm_settings()
    return settings.client_profiles.get(str(client_id), "default") if client_id else "default"


def model_for(purpose: Purpose, client_id: Optional[int] = None, settings: Optional[LLMSettings] = None) -> str:
    """Gateway alias for this purpose, honouring the client's profile (unknown profile = default)."""
    settings = settings or get_llm_settings()
    default = getattr(settings, f"model_{purpose.value}")
    if purpose == Purpose.EMBEDDING:
        return default
    return settings.profiles.get(profile_of(client_id, settings), {}).get(purpose.value, default)


def model_identity(purpose: Purpose, client_id: Optional[int] = None, settings: Optional[LLMSettings] = None) -> str:
    """Alias + config version: changes whenever the model behind the alias may have changed (use in cache keys)."""
    settings = settings or get_llm_settings()
    return f"{model_for(purpose, client_id, settings)}@{settings.config_version}"
