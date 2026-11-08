"""Constants of the document agent (values used in more than one place)."""
from enum import StrEnum

# Magic-byte signatures: we trust the bytes, not the client's extension or declared content type.
PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"            # DOCX and XLSX are both ZIP (OOXML) containers
EMPTY_ZIP_MAGIC = b"PK\x05\x06"     # an empty archive still starts valid

# Entries inside the OOXML ZIP that identify the concrete format.
DOCX_MARKER = "word/document.xml"
XLSX_MARKER = "xl/workbook.xml"

# Content types on the wire (what callers declare; validated against the bytes).
CONTENT_TYPE_PDF = "application/pdf"
CONTENT_TYPE_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CONTENT_TYPE_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class DocumentKind(StrEnum):
    """Concrete document format detected from the file bytes (also a low-cardinality metric label)."""
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"


# Default allowed content types (configurable via settings.allowed_content_types).
DEFAULT_ALLOWED_CONTENT_TYPES = f"{CONTENT_TYPE_PDF},{CONTENT_TYPE_DOCX},{CONTENT_TYPE_XLSX}"
