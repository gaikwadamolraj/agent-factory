import pymupdf


class InvalidPdf(ValueError):
    pass


def parse_pdf(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text from PDF bytes. Blocking: run in an executor. Returns (text, page_count).

    Raises InvalidPdf when the bytes are not a readable PDF.
    """
    if not pdf_bytes.startswith(b"%PDF"):
        raise InvalidPdf("The file is not a PDF.")
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise InvalidPdf(f"Could not read the PDF: {e}") from e
    try:
        return "".join(page.get_text() for page in doc), doc.page_count
    finally:
        doc.close()
