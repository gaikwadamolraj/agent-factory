"""Word (.docx) extraction with python-docx: body paragraphs as text, tables as structured rows, core metadata."""
import io

from docx import Document as DocxDocument

from agent_common.errors import AppError

from agent_document.errors import DocumentErrors
from agent_document.extractors.base import Extracted, ExtractedTable, ExtractLimits, clamp_row, clamp_text


def _metadata(doc) -> dict:
    """Core document properties (no secrets); only non-empty string fields are kept."""
    props = doc.core_properties
    fields = {"title": props.title, "author": props.author, "subject": props.subject,
              "keywords": props.keywords, "category": props.category, "lastModifiedBy": props.last_modified_by}
    return {key: value for key, value in fields.items() if value}


def _tables(doc, limits: ExtractLimits) -> list[ExtractedTable]:
    """Each Word table -> an ExtractedTable of stringified cell text, capped by the limits."""
    tables: list[ExtractedTable] = []
    for table_index, table in enumerate(doc.tables, start=1):
        if len(tables) >= limits.max_tables:
            break
        source_rows = table.rows
        truncated = len(source_rows) > limits.max_table_rows
        rows = [clamp_row([cell.text for cell in row.cells], limits.max_table_cols)
                for row in source_rows[: limits.max_table_rows]]
        tables.append(ExtractedTable(name=f"table {table_index}", rows=rows, truncated=truncated))
    return tables


def extract_docx(data: bytes, limits: ExtractLimits) -> Extracted:
    """Extract paragraph text, tables and metadata from DOCX bytes. Blocking. Raises UNREADABLE on a broken file."""
    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception as e:
        raise AppError(DocumentErrors.UNREADABLE) from e

    lines = [paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()]
    tables = _tables(doc, limits)
    # Append table contents to the text too, so table cells are searchable even though they are also returned
    # structured. One line per row, cells tab-separated.
    for table in tables:
        for row in table.rows:
            if any(cell.strip() for cell in row):
                lines.append("\t".join(row))

    text, truncated = clamp_text("\n".join(lines), limits.max_text_chars)
    return Extracted(text=text, tables=tables, metadata=_metadata(doc), truncated=truncated)
