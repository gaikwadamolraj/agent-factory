"""Detect the real document format from the file bytes (zero trust: never trust the extension or declared type).

PDFs start with %PDF. DOCX and XLSX are both OOXML (ZIP) containers, told apart by the entries they contain.
"""
import io
import zipfile

from agent_common.errors import AppError

from agent_document.constants import (
    DOCX_MARKER,
    EMPTY_ZIP_MAGIC,
    PDF_MAGIC,
    XLSX_MARKER,
    ZIP_MAGIC,
    DocumentKind,
)
from agent_document.errors import DocumentErrors


def _zip_kind(data: bytes) -> DocumentKind:
    """Look inside the OOXML ZIP to decide DOCX vs XLSX. Raises UNSUPPORTED_TYPE for other/unknown archives."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as e:
        raise AppError(DocumentErrors.UNREADABLE) from e
    if DOCX_MARKER in names:
        return DocumentKind.DOCX
    if XLSX_MARKER in names:
        return DocumentKind.XLSX
    raise AppError(DocumentErrors.UNSUPPORTED_TYPE)


def detect_kind(data: bytes) -> DocumentKind:
    """Return the concrete format of `data` from its magic bytes. Raises an AppError when it is none of the three."""
    if not data:
        raise AppError(DocumentErrors.NO_CONTENT)
    if data.startswith(PDF_MAGIC):
        return DocumentKind.PDF
    if data.startswith(ZIP_MAGIC) or data.startswith(EMPTY_ZIP_MAGIC):
        return _zip_kind(data)
    raise AppError(DocumentErrors.UNSUPPORTED_TYPE)
