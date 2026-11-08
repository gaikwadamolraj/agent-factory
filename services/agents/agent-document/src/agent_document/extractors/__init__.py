"""Per-format extractors, and the tier chosen per document.

Each extractor is a plain, blocking function with no framework or network dependency, so it can be reused
and tested on its own; the service layer runs it in a thread pool.

PDFs are read at one of three tiers, decided per document by `routing.choose` - a resume does not need
layout analysis, a policy wording does, and a scan needs OCR. DOCX and XLSX are already structured, so they
go straight to their dedicated readers. See `routing.py` for why, and docs/impl_docling.md for the costs.

A tier that is configured but not installed, or that fails for a reason that is not the document's fault,
degrades to the tier below with a log line. A *broken document* still raises: that is a real failure and the
caller must be told, never handed a plausible-looking empty result.
"""
from typing import Optional

from agent_common.logging import get_logger, log_event

from agent_document.constants import DocumentKind
from agent_document.extractors.base import Extracted, ExtractLimits
from agent_document.extractors.docx import extract_docx
from agent_document.extractors.pdf import extract_pdf
from agent_document.extractors.routing import (AUTO, DOCLING, FAST, TABLES, Decision, RoutingPolicy, choose)
from agent_document.extractors.xlsx import extract_xlsx

log = get_logger(__name__)

# One dispatch table so the service does not branch on the kind.
EXTRACTORS = {
    DocumentKind.PDF: extract_pdf,
    DocumentKind.DOCX: extract_docx,
    DocumentKind.XLSX: extract_xlsx,
}


def docling_available() -> bool:
    """Is the optional Docling extra installed in this image? Checked without importing the heavy parts."""
    from importlib.util import find_spec

    try:
        return find_spec("docling") is not None
    except (ImportError, ValueError):           # pragma: no cover - a broken install is "not available"
        return False


def _run_tier(decision: Decision, kind: DocumentKind, data: bytes, limits: ExtractLimits,
              artifacts_path: str) -> Extracted:
    """Run one tier. Raises `DoclingUnavailable` / `ImportError` for the caller to degrade on."""
    if decision.backend == DOCLING:
        from agent_document.extractors.docling_backend import extract_with_docling
        return extract_with_docling(kind, data, limits, artifacts_path=artifacts_path, ocr=decision.ocr)
    if decision.backend == TABLES and kind is DocumentKind.PDF:
        from agent_document.extractors.pdfplumber_backend import extract_with_pdfplumber
        return extract_with_pdfplumber(data, limits)
    return EXTRACTORS[kind](data, limits)


def extract(kind: DocumentKind, data: bytes, limits: ExtractLimits, configured: str = AUTO, *,
            artifacts_path: str = "", ocr: bool = False, policy: Optional[RoutingPolicy] = None) -> Extracted:
    """Extract `data` at the tier this document needs (blocking; run in an executor).

    `configured` is the DOCUMENT_EXTRACTOR setting: a tier name pins it, `auto` probes the document. `ocr`
    applies only when the docling tier is pinned - in `auto` the probe decides, because whether a page needs
    OCR is a fact about the page.

    The chosen tier and the reason are recorded in `metadata["extractionTier"]` / `["extractionReason"]`, so
    a slow document can be explained afterwards without reproducing it.
    """
    available = docling_available()
    decision = choose(kind, data, configured, policy or RoutingPolicy(), docling_available=available)
    if decision.backend == DOCLING and configured == DOCLING and ocr:
        decision = Decision(DOCLING, ocr=True, reason=decision.reason)

    try:
        extracted = _run_tier(decision, kind, data, limits, artifacts_path)
    except ImportError as error:
        # The tier is not installed in this image. The document is still worth reading, with less structure.
        log_event(log, "document.tier_unavailable", kind=kind.value, tier=decision.backend,
                  reason=str(error)[:200])
        decision = Decision(FAST, reason=f"{decision.backend} is not installed in this image")
        extracted = EXTRACTORS[kind](data, limits)

    extracted.metadata.setdefault("extractionTier", decision.backend)
    if decision.reason:
        extracted.metadata.setdefault("extractionReason", decision.reason)
    log_event(log, "document.extracted", kind=kind.value, tier=decision.backend, ocr=decision.ocr,
              chars=len(extracted.text), tables=len(extracted.tables))
    return extracted


__all__ = ["AUTO", "DOCLING", "FAST", "TABLES", "Decision", "Extracted", "ExtractLimits", "RoutingPolicy",
           "docling_available", "extract", "extract_pdf", "extract_docx", "extract_xlsx"]
