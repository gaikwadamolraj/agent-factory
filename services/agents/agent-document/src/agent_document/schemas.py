"""Wire contracts of the document agent's MCP tools. snake_case in Python, camelCase on the wire (ApiModel/ApiResponse).

The safety scan reuses the shared guardrails `TextSource` enum so document sources line up with the rest of the
platform (resume, job_description, document, ...).
"""
from typing import Optional

from pydantic import Field

from agent_common.guardrails.contracts import TextSource
from agent_common.models import ApiModel, ApiResponse

from agent_document.constants import DocumentKind

# A base64 string is ~4/3 the size of its bytes; bound the field so an oversized payload is rejected before decoding.
# The exact decoded-size check happens in code against settings.max_file_bytes.
MAX_DATA_BASE64_CHARS = 32 * 1024 * 1024    # ~24 MiB decoded; the real limit is settings.max_file_bytes


# ---------------------------------------------------------------- read_document
class ReadDocumentRequest(ApiModel):
    filename: str = Field(min_length=1, max_length=255, description="Original file name (used only for logging/labels).")
    content_type: str = Field(default="", max_length=255,
                              description="Declared MIME type; validated against the real bytes, never trusted alone.")
    data_base64: str = Field(min_length=1, max_length=MAX_DATA_BASE64_CHARS,
                             description="The file bytes, base64-encoded.")
    scan: bool = Field(default=False,
                       description="When true, also injection-scan the extracted text and return `safety`.")


class Table(ApiResponse):
    """A structured table. For XLSX one table = one sheet; for DOCX/PDF one table = one detected table."""
    name: str = Field(description="Sheet name (xlsx) or 'page N table M' (pdf) / 'table M' (docx).")
    rows: list[list[str]] = Field(default_factory=list, description="Row-major cells, everything stringified.")
    truncated: bool = Field(default=False, description="True when rows/columns were capped by the limits.")


class SafetyResult(ApiResponse):
    """Result of the injection scan (mirrors the guardrails service's CheckResult, trimmed to what callers need)."""
    blocked: bool                                       # True = do not let this text reach an LLM
    score: float = 0.0                                  # classifier probability of injection (max over chunks)
    reasons: list[str] = Field(default_factory=list)    # short, user-safe reasons
    detector: str = ""                                  # which detector produced the result


class ReadDocumentResult(ApiResponse):
    kind: DocumentKind
    text: str = ""
    pages: Optional[int] = Field(default=None, description="Page count (PDF only).")
    sheets: Optional[list[str]] = Field(default=None, description="Sheet names in order (XLSX only).")
    tables: list[Table] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict, description="Format metadata (title, author, counts); no secrets.")
    truncated: bool = Field(default=False, description="True when the extracted text was cut at the character limit.")
    safety: Optional[SafetyResult] = Field(default=None, description="Present only when the request asked to scan.")


# ---------------------------------------------------------------- scan_document
class ScanDocumentRequest(ApiModel):
    text: str = Field(min_length=1, max_length=200_000, description="Text to injection-scan.")
    source: TextSource = Field(default=TextSource.DOCUMENT, description="Where the text came from (a metric label).")


# scan_document returns a SafetyResult (defined above).


# ---------------------------------------------------------------- chunk_document
class ChunkDocumentRequest(ApiModel):
    text: str = Field(min_length=1, max_length=2_000_000, description="Text to split into overlapping chunks.")
    chunk_size: Optional[int] = Field(default=None, ge=100, le=20_000,
                                      description="Target chunk size in characters (default from settings).")
    chunk_overlap: Optional[int] = Field(default=None, ge=0, le=5_000,
                                         description="Overlap between chunks in characters (default from settings).")
    embed: bool = Field(default=True, description="Also embed the chunks (non-fatal: skipped if embeddings are down).")
    include_vectors: bool = Field(default=False,
                                  description="Return the raw vectors too (large); off by default to keep payloads small.")


class ChunkDocumentResult(ApiResponse):
    chunks: list[str] = Field(default_factory=list)
    count: int = 0
    embedded: bool = Field(default=False, description="True when vectors were produced for the chunks.")
    embedding_id: Optional[str] = Field(default=None, description="Identity of the embedding model (stored with vectors).")
    embedding_dim: Optional[int] = Field(default=None, description="Vector dimension when embedded.")
    vectors: Optional[list[list[float]]] = Field(default=None, description="One vector per chunk (only if requested).")
