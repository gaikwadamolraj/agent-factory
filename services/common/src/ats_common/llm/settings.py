"""LLM settings shared by every service. One ConfigMap (`ats-llm-config`) feeds all of them, so a model change is
made in one place. Services never name a real model: they ask the LLM gateway for an *alias* per purpose, and
the gateway (LiteLLM, deploy/k8s/base/llm-gateway) maps aliases to real models, routes by capacity and falls back.

Environment (all optional except LLM_GATEWAY_URL):
    LLM_GATEWAY_URL            http://ats-llm-gateway/v1
    LLM_GATEWAY_KEY            from Secret ats-llm-gateway-secrets
    LLM_MODEL_CHAT ... _EMBEDDING   gateway alias per purpose (defaults below)
    LLM_PROFILES               JSON: profile -> {purpose: alias}, overrides per profile
    LLM_CLIENT_PROFILES        JSON: client id -> profile name (per-tenant model policy)
    LLM_CONFIG_VERSION         bump when the models behind the aliases change: part of every cache key
"""
import json
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_", extra="ignore", case_sensitive=False, env_file=None)

    gateway_url: str = Field(description="OpenAI-compatible base URL of the LLM gateway")
    gateway_key: SecretStr = SecretStr("not-needed")
    config_version: str = "1"

    # gateway alias per purpose
    model_chat: str = "ats-chat"
    model_screening: str = "ats-screening"
    model_extraction: str = "ats-extraction"
    model_answer: str = "ats-answer"
    model_embedding: str = "ats-embedding"

    # per-tenant policy (Phase C)
    profiles: dict[str, dict[str, str]] = Field(default_factory=dict)
    client_profiles: dict[str, str] = Field(default_factory=dict)

    # embeddings: identity stored next to every vector, dimension, task prefixes (nomic-embed needs them)
    embedding_id: str = "text-embedding-nomic-embed-text-v1.5@q8_0"
    embedding_dim: int = 768
    embedding_document_prefix: str = "search_document: "
    embedding_query_prefix: str = "search_query: "

    # client behaviour (the gateway retries across backends; clients only time out and break the circuit)
    timeout_s: float = 300.0
    embedding_timeout_s: float = 60.0
    max_output_tokens: int = 4096
    breaker_failures: int = 5
    breaker_reset_s: float = 30.0

    @field_validator("profiles", "client_profiles", mode="before")
    @classmethod
    def _json(cls, value):
        return json.loads(value) if isinstance(value, str) and value.strip() else (value or {})


@lru_cache
def get_llm_settings() -> LLMSettings:
    return LLMSettings()


def reset_llm_settings() -> None:
    """Tests only: forget the cached settings."""
    get_llm_settings.cache_clear()
