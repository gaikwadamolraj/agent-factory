"""Embeddings for the whole platform: one model (alias LLM_MODEL_EMBEDDING), one identity (LLM_EMBEDDING_ID stored
next to every vector), one dimension, and the task prefixes the model family needs.

    vectors = await embed_documents(["chunk 1", "chunk 2"])   # "search_document: " prefix
    vector = await embed_query("python experience")            # "search_query: " prefix
    vector = await embed_text("Apache Kafka")                  # no prefix (symmetric use, e.g. graph names/facts)
"""
from typing import Optional

from agent_common.llm.calls import gateway_client, guarded
from agent_common.llm.models import Purpose, model_for
from agent_common.llm.settings import LLMSettings, get_llm_settings


def embedding_id(settings: Optional[LLMSettings] = None) -> str:
    """Identity of the embedding model behind the alias (stored with vectors; search only compares equal ids)."""
    return (settings or get_llm_settings()).embedding_id


async def _embed(texts: list[str], op: str) -> list[list[float]]:
    settings = get_llm_settings()

    async def once() -> list[list[float]]:
        result = await gateway_client().embeddings.create(model=model_for(Purpose.EMBEDDING), input=texts)
        return [item.embedding[: settings.embedding_dim] for item in sorted(result.data, key=lambda d: d.index)]

    return await guarded(Purpose.EMBEDDING, once, op, timeout_s=settings.embedding_timeout_s, retry_malformed=False)


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Vectors for stored passages."""
    prefix = get_llm_settings().embedding_document_prefix
    return await _embed([prefix + t for t in texts], "embeddings.documents") if texts else []


async def embed_query(text: str) -> list[float]:
    """Vector for a search query."""
    return (await _embed([get_llm_settings().embedding_query_prefix + text], "embeddings.query"))[0]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Vectors without a task prefix (when stored items and queries are embedded the same way)."""
    return await _embed(texts, "embeddings.text") if texts else []


async def embed_text(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
