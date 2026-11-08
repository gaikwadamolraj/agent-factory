"""Business logic of the document agent, free of the MCP framework so it can be reused and tested directly.

Pipeline for a read: decode -> size check -> detect format from bytes -> extract (in a bounded thread pool) ->
optionally injection-scan. Chunk/embed is a separate step; embedding is optional and never fatal.
"""
import asyncio
import base64
import binascii
import functools
import time

from agent_common.errors import AppError
from agent_common.guardrails.contracts import CheckResult, TextSource
from agent_common.llm import embed_documents, embedding_id
from agent_common.llm.settings import get_llm_settings
from agent_common.logging import get_logger, log_event
from agent_common.security import ServiceTokenProvider

from agent_document import metrics
from agent_document.chunking import chunk_text
from agent_document.config import DocumentSettings
from agent_document.constants import DocumentKind
from agent_document.detection import detect_kind
from agent_document.errors import DocumentErrors
from agent_document.extractors import Extracted, ExtractLimits, RoutingPolicy, extract
from agent_document.guardrails import build_guardrails
from agent_document.schemas import (
    ChunkDocumentRequest,
    ChunkDocumentResult,
    ReadDocumentRequest,
    ReadDocumentResult,
    SafetyResult,
    ScanDocumentRequest,
    Table,
)

log = get_logger("agent_document")


class DocumentService:
    """Holds the shared runtime objects (guardrails client, parse concurrency limiter) built once at start-up."""

    def __init__(self, settings: DocumentSettings, tokens: ServiceTokenProvider | None = None):
        self._settings = settings
        self._guardrails = build_guardrails(settings, tokens)
        # Parsing is blocking/CPU bound: cap how many files are parsed at once so the pod stays responsive.
        self._parse_slots = asyncio.Semaphore(settings.parse_concurrency)

    # ------------------------------------------------------------------ read
    def _decode(self, data_base64: str) -> bytes:
        """base64 -> bytes with the size limit enforced. Raises INPUT_INVALID / TOO_LARGE / NO_CONTENT."""
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise AppError(DocumentErrors.INPUT_INVALID, detail="dataBase64 is not valid base64.") from e
        if not data:
            raise AppError(DocumentErrors.NO_CONTENT)
        if len(data) > self._settings.max_file_bytes:
            raise AppError(DocumentErrors.TOO_LARGE)
        return data

    def _limits(self) -> ExtractLimits:
        s = self._settings
        return ExtractLimits(max_text_chars=s.max_text_chars, max_tables=s.max_tables,
                             max_table_rows=s.max_table_rows, max_table_cols=s.max_table_cols)

    async def _extract(self, kind: DocumentKind, data: bytes) -> Extracted:
        """Run the blocking extractor in a thread, bounded by the parse-concurrency semaphore and a timeout.

        The timeout frees the *caller*, not the thread: Python cannot interrupt a running C extension, so a
        pathological document keeps one parse slot busy until it finishes. That is bounded - at worst every
        slot is occupied and the service rejects new reads with this same timeout - and it is far better
        than holding a user's request open for minutes. Raises `FILE-TMO-0010`, which is retryable.
        """
        loop = asyncio.get_running_loop()
        settings = self._settings
        policy = RoutingPolicy(min_chars_per_page=settings.route_min_chars_per_page,
                               table_line_threshold=settings.route_table_line_threshold)
        work = functools.partial(extract, kind, data, self._limits(), settings.document_extractor,
                                 artifacts_path=settings.docling_artifacts_path, ocr=settings.docling_ocr,
                                 policy=policy)
        async with self._parse_slots:
            try:
                return await asyncio.wait_for(loop.run_in_executor(None, work),
                                              timeout=settings.extract_timeout_s)
            except asyncio.TimeoutError:
                log_event(log, "document.extract_timeout", kind=kind.value,
                          seconds=settings.extract_timeout_s, bytes=len(data))
                metrics.documents_read_total.add(1, {"kind": kind.value, "outcome": "timeout"})
                raise AppError(DocumentErrors.EXTRACTION_TIMEOUT,
                               retry_after=int(settings.extract_timeout_s)) from None

    @staticmethod
    def _has_content(extracted: Extracted) -> bool:
        """True when the document yielded any text or any non-empty table cell."""
        if extracted.text.strip():
            return True
        return any(cell.strip() for table in extracted.tables for row in table.rows for cell in row)

    async def read(self, request: ReadDocumentRequest) -> ReadDocumentResult:
        """Extract text/tables/metadata from one uploaded file; optionally injection-scan the text."""
        started = time.perf_counter()
        data = self._decode(request.data_base64)
        kind = detect_kind(data)
        try:
            extracted = await self._extract(kind, data)
        except AppError:
            metrics.documents_read_total.add(1, {"kind": kind.value, "outcome": "error"})
            raise
        if not self._has_content(extracted):
            metrics.documents_read_total.add(1, {"kind": kind.value, "outcome": "empty"})
            raise AppError(DocumentErrors.NO_TEXT)

        tables = [Table(name=t.name, rows=t.rows, truncated=t.truncated) for t in extracted.tables]
        result = ReadDocumentResult(kind=kind, text=extracted.text, pages=extracted.pages, sheets=extracted.sheets,
                                    tables=tables, metadata=extracted.metadata, truncated=extracted.truncated)
        if request.scan:
            result.safety = await self.scan(ScanDocumentRequest(text=extracted.text, source=TextSource.DOCUMENT))

        metrics.documents_read_total.add(1, {"kind": kind.value, "outcome": "ok"})
        metrics.read_duration.record(time.perf_counter() - started, {"kind": kind.value})
        log_event(log, "document.read", kind=kind.value, filename=request.filename[-120:], pages=extracted.pages,
                  tables=len(tables), truncated=extracted.truncated, scanned=request.scan)  # never the text itself
        return result

    # ------------------------------------------------------------------ scan
    async def scan(self, request: ScanDocumentRequest) -> SafetyResult:
        """Injection-scan text through the shared guardrails (never raises; degrades to local pattern checks)."""
        check: CheckResult = await self._guardrails.check(request.text[: self._settings.max_text_chars], request.source)
        if check.blocked:
            metrics.injection_flags.add(1, {"source": request.source.value})
            log_event(log, "security.document_flagged", source=request.source.value, score=check.score,
                      reasons=check.reasons, detector=check.detector)  # never the text itself
        return SafetyResult(blocked=check.blocked, score=check.score, reasons=check.reasons, detector=check.detector)

    # ------------------------------------------------------------------ chunk
    async def chunk(self, request: ChunkDocumentRequest) -> ChunkDocumentResult:
        """Chunk text and (optionally) embed the chunks. Embedding failure is non-fatal: chunks are still returned."""
        size = request.chunk_size or self._settings.chunk_size
        overlap = min(request.chunk_overlap if request.chunk_overlap is not None else self._settings.chunk_overlap,
                      size - 1)
        chunks = chunk_text(request.text, size=size, overlap=overlap, max_chunks=self._settings.max_chunks)
        metrics.chunks_total.add(len(chunks))

        result = ChunkDocumentResult(chunks=chunks, count=len(chunks))
        if request.embed and chunks:
            await self._embed(chunks, request.include_vectors, result)
        return result

    async def _embed(self, chunks: list[str], include_vectors: bool, result: ChunkDocumentResult) -> None:
        """Embed chunks through the shared LLM gateway; on any failure leave the result unembedded (non-fatal)."""
        try:
            vectors = await embed_documents(chunks)
        except asyncio.CancelledError:
            raise
        except Exception as e:                  # gateway down, timeout, open circuit: keep the chunks, skip vectors
            metrics.embeddings_skipped.add(1)
            log_event(log, "document.embedding_skipped", error_type=type(e).__name__)
            return
        result.embedded = True
        result.embedding_id = embedding_id()
        result.embedding_dim = get_llm_settings().embedding_dim
        if include_vectors:
            result.vectors = vectors
