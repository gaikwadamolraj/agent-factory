"""Documents (resume / job description): ingest (parse -> store -> chunk -> embed) and hybrid search.

Ingest is idempotent per tenant + kind + text hash: re-uploading the same file reuses the stored document.
Embedding failures are non-fatal: chunks are stored without vectors and keyword search still works.
"""
import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from agent_common.db.repositories import TenantScope
from agent_common.errors import AppError
from agent_common.llm import embed_documents, embed_query, embedding_id
from agent_common.logging import get_logger, log_event
from agent_common.guardrails.contracts import TextSource
from ats_orchestrator import metrics
from ats_orchestrator.container import get_container
from ats_orchestrator.errors import OrchErrors
from ats_orchestrator.models import Document, DocumentKind
from ats_orchestrator.repositories import ChunkHit, ChunkRepository, DocumentRepository
from ats_orchestrator.services.documents.blob_store import blob_store
from ats_orchestrator.services.documents.chunking import chunk_text
from ats_orchestrator.services.pdf import InvalidPdf, parse_pdf

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


async def embed_chunks(texts: list[str]) -> tuple[Optional[list[list[float]]], Optional[str]]:
    """Chunk vectors + the embedding identity; (None, None) when embeddings are unavailable (keyword search still works)."""
    try:
        return await embed_documents(texts), embedding_id()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_event(log, "documents.embedding_skipped", error_type=type(e).__name__)
        return None, None


async def embed_query_vector(query: str) -> tuple[Optional[list[float]], Optional[str]]:
    """Query vector + the embedding identity; (None, None) when embeddings are unavailable."""
    try:
        return await embed_query(query), embedding_id()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_event(log, "documents.query_embedding_skipped", error_type=type(e).__name__)
        return None, None


GUARD_SOURCE = {DocumentKind.RESUME: TextSource.RESUME, DocumentKind.JD: TextSource.JOB_DESCRIPTION}


async def reject_injected(kind: DocumentKind, text_content: str) -> None:
    """Refuse a document whose text tries to instruct the AI, as judged by agent-guardrail (NeMo rail: pattern
    checks + injection classifier; pattern checks only when the service is not configured or down). Covers fake
    system banners, override and scoring orders, other languages, base64-hidden orders and white/hidden PDF text.
    Raised before anything is stored, indexed, sent to the knowledge graph or screened. Raises FILE-INV-0004."""
    c = get_container()
    if not c.settings.reject_injected_documents:
        return
    result = await c.guardrails.check(text_content, GUARD_SOURCE.get(kind, TextSource.DOCUMENT))
    if result.blocked:
        metrics.injection_flags.add(1, {"source": f"upload_{kind.value}"})
        log_event(log, "security.document_rejected", kind=kind.value, reasons=result.reasons, score=result.score,
                  detector=result.detector)
        raise AppError(OrchErrors.FILE_INSTRUCTIONS)


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
    await reject_injected(kind, text_content)
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
    vectors, model = await embed_chunks(chunks)
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


def chat_document_ids(chat, source: str = "both") -> list[int]:
    """Ids of the chat's resume and/or JD documents. `source`: resume | jd | both."""
    pairs = ((DocumentKind.RESUME, chat.resume_document_id), (DocumentKind.JD, chat.jd_document_id))
    return [doc_id for kind, doc_id in pairs if doc_id and source in (kind.value, "both")]


async def search(scope: TenantScope, document_ids: list[int], query: str, limit: int = 5,
                 pin_headers: bool = True) -> list[ChunkHit]:
    """Hybrid search (vector + keyword, fused with RRF) over the given documents of the tenant.

    With `pin_headers`, the first chunk of each document comes first (once): it holds the name, location and
    contact details, which similarity search does not connect to questions such as "what is the candidate's name".
    """
    c = get_container()
    vector, model = await embed_query_vector(query)
    async with c.db.session() as session:
        chunks = ChunkRepository(session, scope)
        hits = await chunks.hybrid_search(document_ids, query, vector, model, limit=limit)
        headers = await chunks.header_chunks(document_ids) if pin_headers else []
    return merge_hits(headers, hits)


def merge_hits(pinned: list[ChunkHit], ranked: list[ChunkHit]) -> list[ChunkHit]:
    """Pinned chunks first, then ranked ones, without duplicates (same document and chunk index)."""
    seen, merged = set(), []
    for hit in [*pinned, *ranked]:
        if (hit.document_id, hit.chunk_index) not in seen:
            seen.add((hit.document_id, hit.chunk_index))
            merged.append(hit)
    return merged


def format_hits(hits: list[ChunkHit], max_chars: int) -> str:
    """Excerpts for an LLM prompt, labelled `[resume #0]`, cut at `max_chars` in total."""
    parts, used = [], 0
    for hit in hits:
        part = f"[{hit.kind} #{hit.chunk_index}] {hit.content}"
        if used + len(part) > max_chars:
            break
        parts.append(part)
        used += len(part)
    return "\n\n".join(parts)
