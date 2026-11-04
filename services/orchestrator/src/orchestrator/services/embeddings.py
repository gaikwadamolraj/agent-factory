"""Embeddings served by the LLM host (no HuggingFace download), via LangChain's OpenAIEmbeddings.

The model is discovered from GET {EMBEDDING_ENDPOINT}/models; Q8_0 is preferred because it measured practically
identical to full precision (cosine 0.9998) at about half the latency, while Q4 variants lose retrieval quality.
"""
import re
import threading
from dataclasses import dataclass

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from openai import OpenAI

from ats_common.logging import get_logger, log_event
from orchestrator.config import OrchestratorSettings

log = get_logger(__name__)

_PRECISION_RANKS: list[tuple[tuple[str, ...], int]] = [
    (("q8", "int8", "8bit"), 0),
    (("f16", "fp16", "bf16", "f32", "fp32"), 1),
    (("q6",), 3),
    (("q5",), 4),
    (("q4", "iq4", "int4", "4bit"), 5),
    (("q3", "q2", "iq3", "iq2", "iq1"), 6),
]
_UNKNOWN_PRECISION_RANK = 2
_TASK_PREFIXES: dict[str, tuple[str, str]] = {"nomic-embed": ("search_document: ", "search_query: ")}
_EMBEDDING_HINTS = ("embed", "bge-", "gte-", "e5-")


def is_embedding_model(model_id: str) -> bool:
    return any(hint in model_id.lower() for hint in _EMBEDDING_HINTS)


def precision_rank(model_id: str) -> int:
    tokens = re.split(r"[@:/\-.\s]+", model_id.lower())
    for prefixes, rank in _PRECISION_RANKS:
        if any(token.startswith(prefixes) for token in tokens):
            return rank
    return _UNKNOWN_PRECISION_RANK


def pick_embedding_model(model_ids: list[str]) -> str:
    candidates = [m for m in model_ids if is_embedding_model(m)]
    if not candidates:
        raise RuntimeError(f"No embedding model is served by the LLM host. Available: {model_ids}")
    return min(candidates, key=lambda m: (precision_rank(m), m))


class TaskPrefixedEmbeddings(Embeddings):
    """Adds the document / query prefixes some embedding families need for clean results."""

    def __init__(self, inner: Embeddings, document_prefix: str = "", query_prefix: str = ""):
        self.inner, self.document_prefix, self.query_prefix = inner, document_prefix, query_prefix

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents([self.document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(self.query_prefix + text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.inner.aembed_documents([self.document_prefix + t for t in texts])

    async def aembed_query(self, text: str) -> list[float]:
        return await self.inner.aembed_query(self.query_prefix + text)


@dataclass(frozen=True)
class EmbeddingClient:
    model: str
    embeddings: Embeddings


_client: EmbeddingClient | None = None
_lock = threading.Lock()


def get_embedding_client(settings: OrchestratorSettings) -> EmbeddingClient:
    """Blocking on first use (one /models call); cached for the process lifetime."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            model = settings.embedding_model
            if not model:
                available = [m.id for m in OpenAI(base_url=settings.embeddings_url, api_key=settings.llm_api_key,
                                                  timeout=15, max_retries=1).models.list()]
                model = pick_embedding_model(available)
                log_event(log, "embeddings.model_selected", model=model, available=available)
            inner = OpenAIEmbeddings(model=model, base_url=settings.embeddings_url, api_key=settings.llm_api_key,
                                     check_embedding_ctx_length=False, tiktoken_enabled=False, timeout=120, max_retries=2)
            doc_prefix, query_prefix = next((p for f, p in _TASK_PREFIXES.items() if f in model.lower()), ("", ""))
            _client = EmbeddingClient(model, TaskPrefixedEmbeddings(inner, doc_prefix, query_prefix))
    return _client
