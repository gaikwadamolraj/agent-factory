"""pdfplumber extraction: the middle tier - real table structure for digital PDFs, without Docling's models.

The fast tier (pypdfium2) returns text and nothing else, so a ruled table arrives as a wall of cells in
reading order and whatever meaning the rows carried is gone. The docling tier recovers that meaning, but it
pays for layout models: a multi-gigabyte image and seconds per document. Between those sits the large class
of documents that are *born digital* and already draw their tables with lines - exported statements, rate
cards, policy schedules. For those the geometry is in the file, so the lines can simply be read back.

pdfplumber (MIT) on pdfminer.six (MIT) does exactly that: pure Python, milliseconds to low seconds, no model
weights to ship, and licensing a hosted multi-tenant product can carry. It is also honest about its limits.

**This tier does not do OCR.** It reads the text and vector lines a PDF already contains, so a scanned page -
an image of a table - yields no text and no tables here, however clear it looks to a human. Scans, and pages
whose tables are implied by whitespace rather than drawn, belong to the docling tier (docs/impl_docling.md).
"""
import io

import pdfplumber

from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event

from agent_document.errors import DocumentErrors
from agent_document.extractors.base import (Extracted, ExtractedTable, ExtractLimits, clamp_row, clamp_text)

log = get_logger(__name__)

# Metadata fields worth keeping: short, non-binary, and never document content. Same set as the fast tier,
# so a document reports the same metadata whichever tier read it.
_METADATA_KEYS = ("Title", "Author", "Subject", "Keywords", "Creator", "Producer")

# A metadata value is a label, not content; anything longer is malformed or padding and is cut here.
_MAX_METADATA_CHARS = 500


def _metadata(raw: dict) -> dict:
    """Non-empty string metadata only, keyed in lower case.

    Input is `pdf.metadata`, the document information dictionary as pdfminer decoded it. Returns a plain
    dict. Values that are not strings - dates, byte strings, PostScript name objects - are dropped rather
    than coerced: a date needs a timezone decision this layer should not make, and bytes are not displayable.
    No side effects; never raises, because metadata must not decide whether a document can be read.
    """
    kept: dict = {}
    for key in _METADATA_KEYS:
        try:
            value = raw.get(key)
        except Exception:                       # pragma: no cover - a hostile metadata mapping
            continue
        if isinstance(value, str) and value.strip():
            kept[key.lower()] = value[:_MAX_METADATA_CHARS]
    return kept


def _collect_page_tables(page, page_number: int, limits: ExtractLimits,
                         collected: list[ExtractedTable]) -> None:
    """Append the tables drawn on one page to `collected`, within the configured caps.

    Inputs: an open pdfplumber page, its 1-based number, the extraction limits, and the tables gathered so
    far, which this function appends to in place (its only side effect). Returns nothing.

    Nothing here raises. A page whose table detection fails, and a single table that fails while its cells
    are converted, are both skipped with a `document.table_skipped` log line: one awkward table is never
    worth losing the rest of a document, and the text of that page is returned regardless.
    """
    try:
        detected_tables = page.extract_tables()
    except Exception:                           # pragma: no cover - a page pdfminer cannot lay out
        log_event(log, "document.table_skipped", page=page_number)
        return

    for index, raw_rows in enumerate(detected_tables, start=1):
        if len(collected) >= limits.max_tables:
            return
        try:
            was_cut = len(raw_rows) > limits.max_table_rows
            rows = [clamp_row([("" if cell is None else str(cell)) for cell in row], limits.max_table_cols)
                    for row in raw_rows[: limits.max_table_rows]]
        except Exception:                       # pragma: no cover - one odd table must not fail the read
            log_event(log, "document.table_skipped", page=page_number, table=index)
            continue
        collected.append(ExtractedTable(name=f"page {page_number} table {index}", rows=rows,
                                        truncated=was_cut))


def extract_with_pdfplumber(data: bytes, limits: ExtractLimits) -> Extracted:
    """Extract text, ruled tables, page count and metadata from PDF bytes. Blocking: run in an executor.

    Inputs: the raw PDF bytes and the configured caps. Returns an `Extracted` whose `text` is the pages'
    text joined by newlines (clamped to `limits.max_text_chars`, with `truncated` set when it was cut),
    whose `tables` hold at most `limits.max_tables` tables named `page N table M`, and whose metadata
    carries `pageCount`, `extractor` and any usable document information fields.

    Side effects: none beyond log lines for skipped tables. Raises `AppError(DocumentErrors.UNREADABLE)`
    when the file cannot be opened or its text cannot be read at all - corrupt, encrypted or not a PDF.
    Tables are best-effort by comparison, so a document with no detectable tables is a success with none.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_count = len(pdf.pages)
            metadata = _metadata(pdf.metadata or {})
            page_texts: list[str] = []
            tables: list[ExtractedTable] = []
            for page_number, page in enumerate(pdf.pages, start=1):
                try:
                    page_texts.append(page.extract_text() or "")
                    if len(tables) < limits.max_tables:
                        _collect_page_tables(page, page_number, limits, tables)
                finally:
                    # pdfminer keeps every character object of a page alive in the page cache. Releasing it
                    # as we go keeps peak memory proportional to the largest page, not the whole document.
                    page.close()
            text = "\n".join(page_texts)
    except Exception as error:
        raise AppError(DocumentErrors.UNREADABLE) from error

    text = text.replace("\r\n", "\n").replace("\r", "\n")     # stored text is LF, whatever the producer wrote
    text, truncated = clamp_text(text, limits.max_text_chars)
    metadata["pageCount"] = page_count
    metadata["extractor"] = "pdfplumber"
    return Extracted(text=text, pages=page_count, tables=tables, metadata=metadata, truncated=truncated)
