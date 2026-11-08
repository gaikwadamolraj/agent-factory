"""PDF text extraction.

Uses **pypdfium2** (BSD-3-Clause / Apache-2.0, Google's PDFium behind a thin binding) rather than PyMuPDF,
which is dual-licensed AGPL-3.0 / Artifex commercial. AGPL section 13 covers use over a network, so it is not
a dependency a hosted multi-tenant product can carry without a commercial licence. Layout, tables and OCR are
not this module's job: that is agent-document's extraction capability (docs/impl_docling.md).
"""
import pypdfium2 as pdfium


class InvalidPdf(ValueError):
    pass


def parse_pdf(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text from PDF bytes. Blocking: run in an executor. Returns (text, page_count).

    Pages are joined with a newline so the last word of one page and the first of the next cannot merge into
    a term that is in neither - which would then be "found" in the resume by a grounding check. PDFium
    reports CRLF line endings; they are normalised to LF so stored text matches every other extractor's.

    Unlike PyMuPDF, PDFium also returns text drawn outside the visible page area. That is deliberate and
    better: text hidden off-page is a standard way to smuggle instructions into a resume, and the injection
    scan only sees what extraction returns (docs/security_llm.md).

    Raises InvalidPdf when the bytes are not a readable PDF.
    """
    if not pdf_bytes.startswith(b"%PDF"):
        raise InvalidPdf("The file is not a PDF.")
    try:
        document = pdfium.PdfDocument(pdf_bytes)
    except Exception as error:
        raise InvalidPdf(f"Could not read the PDF: {error}") from error
    try:
        pages = []
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            try:
                pages.append(textpage.get_text_range())
            finally:
                textpage.close()
                page.close()
        return "\n".join(pages).replace("\r\n", "\n").replace("\r", "\n"), len(document)
    except Exception as error:
        raise InvalidPdf(f"Could not read the PDF: {error}") from error
    finally:
        document.close()
