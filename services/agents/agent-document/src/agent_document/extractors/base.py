"""Shared types for the extractors and small helpers for applying the configured limits."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ExtractLimits:
    """Caps applied while extracting, so one file can never blow up memory or the response. From settings."""
    max_text_chars: int
    max_tables: int
    max_table_rows: int
    max_table_cols: int


@dataclass
class ExtractedTable:
    """A structured table in extraction form (converted to the wire `Table` by the service)."""
    name: str
    rows: list[list[str]] = field(default_factory=list)
    truncated: bool = False


@dataclass
class Extracted:
    """Result of reading one document, independent of the wire schema."""
    text: str = ""
    pages: Optional[int] = None                 # PDF page count
    sheets: Optional[list[str]] = None          # XLSX sheet names
    tables: list[ExtractedTable] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    truncated: bool = False                      # text was cut at max_text_chars


def clamp_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut `text` to `max_chars`. Returns (text, truncated)."""
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def clamp_row(cells: list[str], max_cols: int) -> list[str]:
    """Trim a row to at most `max_cols` cells."""
    return cells[:max_cols]
