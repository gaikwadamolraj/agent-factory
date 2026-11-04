"""Documents (resume / job description): ingest (parse -> store -> chunk -> embed) and hybrid search.

Ingest is idempotent per tenant + kind + text hash: re-uploading the same file reuses the stored document.
Embedding failures are non-fatal: chunks are stored without vectors and keyword search still works.
"""
import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from ats_common.db.repositories import TenantScope
from ats_common.errors import AppError
from ats_common.logging import get_logger, log_event
from ats_common.resilience import BreakerConfig, RetryPolicy, get_breaker, resilient_call
from orchestrator.config import OrchestratorSettings
from orchestrator.container import get_container
from orchestrator.errors import OrchErrors
from orchestrator.models import Document, DocumentKind
from orchestrator.repositories import ChunkHit, ChunkRepository, DocumentRepository
from orchestrator.services.documents.blob_store import blob_store
from orchestrator.services.documents.chunking import chunk_text
from orchestrator.services.embeddings import get_embedding_client
from orchestrator.services.pdf import InvalidPdf, parse_pdf

log = get_logger(__name__)
PDF_TYPE = "application/pdf"
TEXT_TYPES = {"text/plain", "text/markdown"}


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_type: str
    data: bytes


def _extract_text(upload: UploadedFile) -> tuple[str, int]:
    """Blocking: PDF -> text (pages); text files -> decoded text (1 page)."""
    if upload.content_type == PDF_TYPE:
        return parse_pdf(upload.data)
    try:
        return upload.data.decode("utf-8"), 1
    except UnicodeDecodeError:
        raise InvalidPdf("Text file is not UTF-8")


def detect_type(filename: str, declared: Optional[str], data: bytes, allow_text: bool) -> str:
    """Trust the bytes, not the client: PDFs must start with %PDF; text only where allowed (JD)."""
    name = filename.lower()
    if data.startswith(b"%PDF") and name.endswith(".pdf"):
        return PDF_TYPE
    if allow_text and name.endswith((".txt", ".md")) and b"\x00" not in data[:4096]:
        return "text/markdown" if name.endswith(".md") else "text/plain"
    raise AppError(OrchErrors.FILE_NOT_PDF if not allow_text else OrchErrors.FILE_TYPE_NOT_ALLOWED)


async def _embed(settings: OrchestratorSettings, texts: list[str]) -> tuple[Optional[list[list[float]]], Optional[str]]:
    """Embeddings for chunks (resilient; returns (None, None) when the embedding service is unavailable)."""
    loop = asyncio.get_running_loop()
    breaker = get_breaker("embeddings", BreakerConfig(3, 60))
    try:
        client = await loop.run_in_executor(None, get_embedding_client, settings)
        vectors = await resilient_call(lambda: loop.run_in_executor(None, client.embeddings.embed_documents, texts),
                                       op="embeddings.documents", timeout_s=120, retry=RetryPolicy(attempts=2),
                                       breaker=breaker)
        return vectors, client.model
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_event(log, "documents.embedding_skipped", error_type=type(e).__name__)
        return None, None


async def embed_query(settings: OrchestratorSettings, query: str) -> tuple[Optional[list[float]], Optional[str]]:
    loop = asyncio.get_running_loop()
    try:
        client = await loop.run_in_executor(None, get_embedding_client, settings)
        vector = await resilient_call(lambda: loop.run_in_executor(None, client.embeddings.embed_query, query),
                                      op="embeddings.query", timeout_s=30, breaker=get_breaker("embeddings"))
        return vector, client.model
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_event(log, "documents.query_embedding_skipped", error_type=type(e).__name__)
        return None, None


async def ingest(scope: TenantScope, user_id: int, kind: DocumentKind, upload: UploadedFile) -> tuple[Document, bool]:
    """Store a resume/JD file and index it for hybrid search. Returns (document, created)."""
    c = get_container()
    started = time.perf_counter()
    try:
        text_content, pages = await asyncio.get_running_loop().run_in_executor(None, _extract_text, upload)
    except InvalidPdf:
        raise AppError(OrchErrors.FILE_NOT_PDF)
    if not text_content.strip():
        raise AppError(OrchErrors.FILE_NO_TEXT)
    text_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()

    async with c.db.session() as session:
        documents = DocumentRepository(session, scope)
        existing = await documents.find_by_hash(kind.value, text_hash)
        if existing:
            return existing, False
        blob_key = f"{kind.value}-{text_hash[:40]}-{scope.client_id}-{scope.project_id}"
        await blob_store(c.settings.blob_backend, session).put(scope, blob_key, upload.data)
        document = await documents.add(Document(
            client_id=scope.client_id, project_id=scope.project_id, user_id=user_id, kind=kind.value,
            filename=upload.filename[-255:], content_type=upload.content_type, size_bytes=len(upload.data),
            pages=pages, text=text_content, text_hash=text_hash, blob_key=blob_key))

    chunks = chunk_text(text_content)
    vectors, model = await _embed(c.settings, chunks)
    async with c.db.session() as session:
        await ChunkRepository(session, scope).replace_chunks(document, chunks, vectors, model)
    log_event(log, "documents.ingested", kind=kind.value, pages=pages, chunks=len(chunks), embedded=bool(vectors),
              duration_ms=round((time.perf_counter() - started) * 1000))
    return document, True


async def ingest_text(scope: TenantScope, user_id: int, kind: DocumentKind, text_content: str) -> Document:
    """Index pasted text (e.g. a JD typed in the panel) the same way as an uploaded file."""
    upload = UploadedFile(filename=f"{kind.value}.txt", content_type="text/plain", data=text_content.encode("utf-8"))
    document, _ = await ingest(scope, user_id, kind, upload)
    return document


async def search(scope: TenantScope, document_ids: list[int], query: str, limit: int = 5) -> list[ChunkHit]:
    """Hybrid search (vector + keyword, fused with RRF) over the given documents of the tenant."""
    c = get_container()
    vector, model = await embed_query(c.settings, query)
    async with c.db.session() as session:
        return await ChunkRepository(session, scope).hybrid_search(document_ids, query, vector, model, limit=limit)
