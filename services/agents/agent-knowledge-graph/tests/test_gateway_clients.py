"""Graphiti client through the gateway: the output budget is capped whatever Graphiti asks for."""
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

from agent_knowledge_graph.shared.graph.gateway_clients import extraction_llm


async def test_graphiti_requests_for_16k_output_are_capped(monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    from agent_common.llm import reset_llm_settings
    reset_llm_settings()
    seen = []

    async def fake_generate(self, messages, response_model=None, max_tokens=16384, model_size=None, *, model=None):
        seen.append(max_tokens)
        return {}

    monkeypatch.setattr(OpenAIGenericClient, "_generate_response", fake_generate)
    client = extraction_llm(1)
    await client._generate_response([], None, 16384)        # edge extraction asks for 16384
    await client._generate_response([], None, 1000)         # smaller requests are kept
    assert seen == [4096, 1000]
