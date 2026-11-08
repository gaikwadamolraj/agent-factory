"""Test environment: a gateway URL for agent_common.llm (model aliases and cache identities are read from it).
No test calls the gateway; LLM calls are faked."""
import pytest
from agent_common.llm import reset_llm_settings


@pytest.fixture(autouse=True)
def llm_settings(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gateway.test/v1")
    reset_llm_settings()
    yield
    reset_llm_settings()
