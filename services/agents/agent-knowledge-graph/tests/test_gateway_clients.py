"""Graphiti client through the gateway: the output budget is this service's, and it caps what Graphiti asks.

The budget is deliberately *not* the platform's LLM_MAX_OUTPUT_TOKENS. Extraction returns a large nested
JSON graph where chat returns a paragraph, and sizing both from one number lets the smaller workload cap
the larger one - at which point the model stops mid-structure and the JSON will not parse.
"""
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

from agent_knowledge_graph.config import get_settings
from agent_knowledge_graph.shared.graph.gateway_clients import extraction_llm


def _capture(monkeypatch) -> list[int]:
    seen: list[int] = []

    async def fake_generate(self, messages, response_model=None, max_tokens=16384, model_size=None, *, model=None):
        seen.append(max_tokens)
        return {}

    monkeypatch.setattr(OpenAIGenericClient, "_generate_response", fake_generate)
    return seen


async def test_requests_are_capped_at_the_extraction_budget(monkeypatch):
    """Graphiti asks for 16384 on edge extraction; the client caps it at this service's own budget."""
    monkeypatch.setenv("EXTRACTION_MAX_OUTPUT_TOKENS", "8192")
    get_settings.cache_clear()
    seen = _capture(monkeypatch)

    client = extraction_llm(1)
    await client._generate_response([], None, 16384)    # edge extraction's own ask
    await client._generate_response([], None, 1000)     # a smaller request is left alone

    assert seen == [8192, 1000]
    get_settings.cache_clear()


async def test_chat_budget_does_not_cap_extraction(monkeypatch):
    """The regression this exists for: a 4096-token chat budget must not truncate a graph extraction."""
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.setenv("EXTRACTION_MAX_OUTPUT_TOKENS", "16384")
    from agent_common.llm import reset_llm_settings
    reset_llm_settings()
    get_settings.cache_clear()
    seen = _capture(monkeypatch)

    client = extraction_llm(1)
    await client._generate_response([], None, 16384)

    assert seen == [16384], "extraction must not inherit the chat output budget"
    get_settings.cache_clear()
    reset_llm_settings()


async def test_graphiti_uses_the_platform_gateway_timeout(monkeypatch):
    """Graphiti must not build its own HTTP client: its default timeout is far shorter than an extraction.

    The symptom when it does is "Error in generating LLM response: Request timed out" while the gateway is
    still generating, so the episode fails having already spent the time.
    """
    monkeypatch.setenv("LLM_TIMEOUT_S", "1800")
    from agent_common.llm import reset_llm_settings
    reset_llm_settings()
    get_settings.cache_clear()

    client = extraction_llm(1)

    assert client.client.timeout == 1800.0, "the gateway timeout must reach Graphiti's HTTP client"
    assert client.client.max_retries == 0, "retries belong to the worker's lease, not the HTTP client"
    get_settings.cache_clear()
    reset_llm_settings()
