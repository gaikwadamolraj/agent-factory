"""Excel (.xlsx) extraction with openpyxl: one structured table per sheet (cell values), plus a flattened text form.

Read-only + data_only so formulas come back as their last computed value and large books stream instead of loading
whole into memory.
"""
import io

from openpyxl import load_workbook

from agent_common.errors import AppError

from agent_document.errors import DocumentErrors
from agent_document.extractors.base import Extracted, ExtractedTable, ExtractLimits, clamp_row, clamp_text


def _cell(value) -> str:
    """Stringify a cell value; None -> empty string."""
    return "" if value is None else str(value)


def _metadata(workbook) -> dict:
    """Workbook core properties; only non-empty string fields are kept."""
    props = workbook.properties
    fields = {"title": props.title, "creator": props.creator, "subject": props.subject,
              "keywords": props.keywords, "category": props.category, "lastModifiedBy": props.lastModifiedBy}
    return {key: value for key, value in fields.items() if value}


def _sheet_table(worksheet, limits: ExtractLimits) -> ExtractedTable:
    """Read one worksheet into an ExtractedTable, capped by the row/column limits."""
    rows: list[list[str]] = []
    truncated = False
    for source_row in worksheet.iter_rows(values_only=True):
        if len(rows) >= limits.max_table_rows:
            truncated = True                    # there is at least one more row we are not returning
            break
        rows.append(clamp_row([_cell(value) for value in source_row], limits.max_table_cols))
    # Drop trailing fully-empty rows that openpyxl reports for formatted-but-empty cells.
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    return ExtractedTable(name=worksheet.title, rows=rows, truncated=truncated)


def extract_xlsx(data: bytes, limits: ExtractLimits) -> Extracted:
    """Extract per-sheet tables, sheet names, a flattened text form and metadata from XLSX bytes. Blocking."""
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise AppError(DocumentErrors.UNREADABLE) from e
    try:
        sheet_names = list(workbook.sheetnames)
        tables = [_sheet_table(ws, limits) for ws in workbook.worksheets[: limits.max_tables]]
        metadata = _metadata(workbook)
    finally:
        workbook.close()

    # Flatten every sheet into text so the content is searchable/scannable: a header line per sheet, tab-separated rows.
    lines: list[str] = []
    for table in tables:
        lines.append(f"# {table.name}")
        for row in table.rows:
            if any(cell.strip() for cell in row):
                lines.append("\t".join(row))

    text, truncated = clamp_text("\n".join(lines), limits.max_text_chars)
    return Extracted(text=text, sheets=sheet_names, tables=tables, metadata=metadata, truncated=truncated)
