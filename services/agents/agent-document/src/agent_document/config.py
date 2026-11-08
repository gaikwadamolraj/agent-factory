"""Document agent settings (environment only; secrets come from the k8s Secret).

Everything that may change per environment - size limits, chunk sizes, the guardrail address, timeouts, breakers
and concurrency - is a setting here, never hard-coded in the code that uses it.
"""
from functools import lru_cache

from agent_common.config import McpServiceSettings

from agent_document.constants import DEFAULT_ALLOWED_CONTENT_TYPES


class DocumentSettings(McpServiceSettings):
    service_name: str = "agent-document"
    port: int = 8006

    # ---- Input limits (validated at the edge, before anything is parsed)
    max_file_bytes: int = 10 * 1024 * 1024      # 10 MiB decoded; bounds memory per request
    max_text_chars: int = 500_000              # extracted text is truncated to this (set `truncated` when cut)
    max_tables: int = 200                      # cap on tables returned (PDF tables + XLSX sheets + DOCX tables)
    max_table_rows: int = 2_000                # cap on rows kept per table
    max_table_cols: int = 100                  # cap on cells kept per row

    # ---- Chunking (self-contained; mirrors the orchestrator's chunker so search behaves the same)
    chunk_size: int = 1_200
    chunk_overlap: int = 200
    max_chunks: int = 2_000                    # bound on chunks produced per request

    # ---- Guardrails (shared agent-guardrail service; empty URL = local pattern checks only)
    guardrail_mcp_url: str = ""                # e.g. http://agent-guardrail/mcp
    guardrail_timeout_s: float = 20.0
    guardrail_breaker_failures: int = 3
    guardrail_breaker_reset_s: float = 30.0

    # ---- Embeddings: optional and non-fatal. Timeout + circuit breaker come from agent_common.llm (the shared LLM
    # gateway settings, ConfigMap ats-llm-config), so there is nothing to configure per service here.

    # ---- Extraction tier, chosen per document (docs/impl_docling.md). How hard a document is to read is a
    # property of the document, not of the deployment, so the default probes each one and picks:
    #   fast    pypdfium2   text only              milliseconds  digital text, no tables
    #   tables  pdfplumber  real table structure   fast          digital text with ruled tables
    #   docling Docling     layout + OCR           seconds       complex layout, or no extractable text
    # Naming a tier here pins it for every document; "auto" decides. Docling is an optional extra: without
    # it installed the router simply never offers that tier.
    document_extractor: str = "auto"           # auto | fast | tables | docling
    docling_artifacts_path: str = ""           # pre-downloaded models; empty = the library's own cache
    docling_ocr: bool = False                  # OCR when the docling tier is *pinned*; auto decides per document
    # One document's parse, whichever tier runs it. Generous because the docling tier on a long scanned PDF
    # genuinely takes minutes on CPU; it exists to stop a pathological file holding a request open forever.
    extract_timeout_s: float = 300.0
    # Probe thresholds. A CV and a scanned ledger disagree about what "sparse" means, so both are settings.
    route_min_chars_per_page: int = 120        # below this: scanned or image-only -> docling with OCR
    route_table_line_threshold: int = 6        # ruled lines per page suggesting a real table -> tables tier

    # ---- Concurrency: parsing is blocking and CPU bound, so it runs in a bounded thread pool.
    parse_concurrency: int = 4

    # ---- Allowed content types (comma-separated; also enforced against the real bytes)
    allowed_content_types: str = DEFAULT_ALLOWED_CONTENT_TYPES

    # ---- Zero trust: who may call which tool (allowed_callers, host, port from McpServiceSettings).
    # Any app that reads documents can be added here and to the k8s oauthClients allow-list.
    allowed_callers: str = "ats-orchestrator"
    tool_policy: str = "ats-orchestrator=read_document|scan_document|chunk_document"

    @property
    def allowed_content_type_set(self) -> set[str]:
        return {t.strip() for t in self.allowed_content_types.split(",") if t.strip()}


@lru_cache
def get_settings() -> DocumentSettings:
    return DocumentSettings()
