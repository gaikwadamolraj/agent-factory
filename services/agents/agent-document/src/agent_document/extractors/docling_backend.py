"""Docling extraction: layout-aware reading order, real table structure and OCR for scanned pages.

Docling (MIT, LF AI & Data) understands a page rather than reading characters off it. That matters for the
documents this platform is growing into - a policy wording whose exclusions are a table, a scanned claim
form, a bank statement - where the light path returns a wall of text in the wrong order and no tables at all.

It is not the default, and the reason is cost, not quality: the models make the image multi-gigabyte and a
document takes seconds rather than milliseconds. A resume does not need layout analysis. Turn it on per
deployment with `DOCUMENT_EXTRACTOR=docling` (docs/impl_docling.md).

**The import is deferred on purpose.** Docling pulls torch and friends at import time; keeping it inside the
function means a deployment on the light path never loads any of it, and an image built without the extra
still starts.
"""
from functools import lru_cache
from typing import Any, Optional

from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event

from agent_document.constants import DocumentKind
from agent_document.errors import DocumentErrors
from agent_document.extractors.base import (Extracted, ExtractedTable, ExtractLimits, clamp_row, clamp_text)

log = get_logger(__name__)

# What Docling is asked to produce, by our document kind. Docling reads far more formats than these three;
# the gate is `detect_kind`, which only admits what the service has declared it accepts.
_SUFFIX = {DocumentKind.PDF: ".pdf", DocumentKind.DOCX: ".docx", DocumentKind.XLSX: ".xlsx"}


class DoclingUnavailable(ImportError):
    """Docling is configured but not importable - the image was built without the extra.

    An `ImportError` on purpose: the dispatcher degrades to a lighter tier on exactly that, so a small image
    is a capability choice rather than a failure mode.
    """


@lru_cache(maxsize=4)
def _converter(artifacts_path: str, ocr: bool):
    """One converter per configuration, built once and reused.

    Building it loads the layout and table models, which takes seconds and a lot of memory; doing that per
    request would make the first document of every request pay for it. `lru_cache` is the bounded cache
    CLAUDE.md asks for - the key space is two settings, so it cannot grow.
    """
    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
    except ImportError as error:                # pragma: no cover - depends on how the image was built
        raise DoclingUnavailable(str(error)) from error

    options = PdfPipelineOptions()
    options.do_ocr = ocr
    options.do_table_structure = True
    if artifacts_path:
        # Models baked into the image at build time: nothing is downloaded on a user's request, which is
        # also what makes the service work in an air-gapped cluster.
        options.artifacts_path = artifacts_path
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def _tables(document: Any, limits: ExtractLimits) -> list[ExtractedTable]:
    """Docling's structured tables, capped by the configured limits."""
    tables: list[ExtractedTable] = []
    for index, table in enumerate(getattr(document, "tables", []) or [], start=1):
        if len(tables) >= limits.max_tables:
            break
        try:
            # `doc` is required since Docling 2.x deprecated the implicit form; the fallback keeps an older
            # pinned version working rather than losing every table on a version skew.
            try:
                frame = table.export_to_dataframe(doc=document)
            except TypeError:                   # pragma: no cover - older Docling
                frame = table.export_to_dataframe()
            raw_rows = [[str(cell) for cell in frame.columns]] + frame.astype(str).values.tolist()
        except Exception:                       # pragma: no cover - one odd table must not fail the read
            log_event(log, "document.table_skipped", table=index)
            continue
        truncated = len(raw_rows) > limits.max_table_rows
        rows = [clamp_row([("" if cell is None else str(cell)) for cell in row], limits.max_table_cols)
                for row in raw_rows[: limits.max_table_rows]]
        tables.append(ExtractedTable(name=f"table {index}", rows=rows, truncated=truncated))
    return tables


def extract_with_docling(kind: DocumentKind, data: bytes, limits: ExtractLimits, *,
                         artifacts_path: str = "", ocr: bool = False) -> Extracted:
    """Extract text (as Markdown), tables and page count with Docling. Blocking: run in an executor.

    Returns Markdown rather than raw text on purpose: headings, lists and tables survive it, which is what
    makes a chunk self-describing later. Raises `DoclingUnavailable` when the extra is not installed, so the
    caller can fall back to the light path instead of failing the user's request.
    """
    from io import BytesIO

    try:
        from docling.datamodel.base_models import DocumentStream
    except ImportError as error:                # pragma: no cover
        raise DoclingUnavailable(str(error)) from error

    converter = _converter(artifacts_path, ocr)
    stream = DocumentStream(name=f"document{_SUFFIX.get(kind, '')}", stream=BytesIO(data))
    try:
        result = converter.convert(stream)
    except Exception as error:
        raise AppError(DocumentErrors.UNREADABLE) from error

    document = result.document
    text = document.export_to_markdown()
    text, truncated = clamp_text(text, limits.max_text_chars)
    pages: Optional[int] = len(getattr(document, "pages", {}) or {}) or None
    metadata: dict = {"extractor": "docling"}
    if pages:
        metadata["pageCount"] = pages
    return Extracted(text=text, pages=pages, tables=_tables(document, limits), metadata=metadata,
                     truncated=truncated)
