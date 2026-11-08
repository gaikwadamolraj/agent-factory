"""PDF extraction with pypdfium2 (BSD-3-Clause / Apache-2.0) - the fast, light path.

PyMuPDF used to do this. It is dual-licensed AGPL-3.0 / Artifex commercial, and AGPL section 13 covers use
over a network, so it is not a dependency a hosted multi-tenant product can carry without buying the
commercial licence. pypdfium2 wraps Google's PDFium and is permissively licensed.

**This path returns no tables.** PyMuPDF's table detection went with it, and a guessed table is worse than
none - a mis-parsed exclusion table in a policy is a wrong answer stated confidently. Real table structure
comes from the Docling backend (`DOCUMENT_EXTRACTOR=docling`, see docs/impl_docling.md), which is built for
it. The light path remains the right default for documents that are simply text, such as most resumes.
"""
import pypdfium2 as pdfium

from agent_common.errors import AppError

from agent_document.errors import DocumentErrors
from agent_document.extractors.base import Extracted, ExtractLimits, clamp_text

# Metadata fields worth keeping: short, non-binary, and never document content.
_METADATA_KEYS = ("Title", "Author", "Subject", "Keywords", "Creator", "Producer")


def _metadata(document: "pdfium.PdfDocument") -> dict:
    """Non-empty string metadata only (no dates, no binary, nothing sensitive)."""
    try:
        raw = document.get_metadata_dict(skip_empty=True)
    except Exception:                           # pragma: no cover - metadata must never fail a read
        return {}
    out = {}
    for key in _METADATA_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key.lower()] = value[:500]
    return out


def _page_text(document: "pdfium.PdfDocument", index: int) -> str:
    page = document[index]
    textpage = page.get_textpage()
    try:
        return textpage.get_text_range()
    finally:
        textpage.close()
        page.close()


def extract_pdf(data: bytes, limits: ExtractLimits) -> Extracted:
    """Extract text, page count and metadata from PDF bytes. Blocking. Raises UNREADABLE on a broken PDF.

    PDFium also returns text drawn outside the visible page area, which PyMuPDF dropped. That is deliberate:
    text hidden off-page is a standard way to smuggle instructions into a document, and the injection scan
    can only judge what extraction returns (docs/security_llm.md).
    """
    try:
        document = pdfium.PdfDocument(data)
    except Exception as error:
        raise AppError(DocumentErrors.UNREADABLE) from error
    try:
        page_count = len(document)
        text = "\n".join(_page_text(document, index) for index in range(page_count))
        metadata = _metadata(document)
    except Exception as error:
        raise AppError(DocumentErrors.UNREADABLE) from error
    finally:
        document.close()

    text = text.replace("\r\n", "\n").replace("\r", "\n")     # PDFium reports CRLF; stored text is LF
    text, truncated = clamp_text(text, limits.max_text_chars)
    metadata["pageCount"] = page_count
    return Extracted(text=text, pages=page_count, tables=[], metadata=metadata, truncated=truncated)
