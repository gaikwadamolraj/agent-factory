"""Graphiti clients that talk to the LLM gateway through agent_common.llm, so extraction uses the same gateway,
model policy and embedding model as the rest of the platform.

* `extraction_llm(client_id)` - Graphiti's OpenAI-compatible LLM client, model = the client's extraction alias.
* `GatewayEmbedder`           - Graphiti EmbedderClient backed by agent_common.llm.embed_texts (no task prefix:
                                 Graphiti embeds stored names/facts and search queries the same way).
"""
from collections.abc import Iterable
from typing import Optional

from agent_common.llm import Purpose, gateway_client_args, get_llm_settings, model_for
from agent_common.llm.embeddings import embed_text, embed_texts
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient


def _llm_config(client_id: Optional[int]) -> LLMConfig:
    args, settings = gateway_client_args(), get_llm_settings()
    model = model_for(Purpose.EXTRACTION, client_id)
    return LLMConfig(api_key=args["api_key"], base_url=args["base_url"], model=model, small_model=model,
                     temperature=0.0, max_tokens=settings.max_output_tokens)


class CappedOutputClient(OpenAIGenericClient):
    """Graphiti's OpenAI-compatible client with every request's output budget capped at LLM_MAX_OUTPUT_TOKENS.

    Graphiti asks for max_tokens=16384 in edge extraction whatever the client is configured with. A backend with an
    8k context then either rejects the request (HTTP 400) or runs out of room mid-answer and returns cut-off JSON
    (seen as "Expecting value: line 1 column 2105"). The gateway routes by prompt size assuming this budget.
    """

    async def _generate_response(self, messages, response_model=None, max_tokens=DEFAULT_MAX_TOKENS,
                                 model_size=ModelSize.medium, *, model=None):
        return await super()._generate_response(messages, response_model, min(max_tokens, self.max_tokens),
                                                model_size, model=model)


def extraction_llm(client_id: Optional[int]) -> OpenAIGenericClient:
    """Graphiti LLM client for one client's graphs (its extraction alias), output capped (CappedOutputClient)."""
    return CappedOutputClient(config=_llm_config(client_id), max_tokens=get_llm_settings().max_output_tokens)


def unused_reranker() -> OpenAIRerankerClient:
    """Graphiti requires a cross-encoder; searches use RRF, so it is never called."""
    return OpenAIRerankerClient(config=_llm_config(None))


class GatewayEmbedder(EmbedderClient):
    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        text = input_data if isinstance(input_data, str) else " ".join(map(str, input_data))
        return await embed_text(text)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return await embed_texts(input_data_list)
