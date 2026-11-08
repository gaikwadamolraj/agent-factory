"""Choosing how hard to read a document, per document.

One extractor for every file is the wrong shape. A digital resume is text and nothing else, and reading it
with a layout model costs seconds and a multi-gigabyte image for an identical answer. A policy wording is a
table, and reading it as a stream of words destroys the association between a clause and its limit - "Flood
damage No 0 Fire damage Yes 250000" is not an answer, it is a hazard. A scanned claim form has no text at
all, and every text reader returns an empty string without saying anything is wrong.

So the backend is a decision, made cheaply, per document:

    fast     pypdfium2    text only              milliseconds   digital text, no tables
    tables   pdfplumber   real table structure   fast           digital text with ruled tables
    docling  Docling      layout + OCR           seconds        complex layout, or no text at all

The probe reads the document once with the cheapest reader and looks at two things: how much text there is
per page, and how many ruled lines the pages contain. Both thresholds are settings, because what counts as
"sparse" differs between a CV and a scanned ledger, and a new tier (a vision model, a different OCR engine)
is added here without touching any caller.

Nothing in this module parses deeply or calls out: it must be cheap enough that paying for it on every
document is obviously worth it.
"""
from dataclasses import dataclass
from typing import Optional

from agent_common.logging import get_logger, log_event

from agent_document.constants import DocumentKind

log = get_logger(__name__)

# Tier names. Also the accepted values of DOCUMENT_EXTRACTOR, plus AUTO to let this module decide.
AUTO = "auto"
FAST = "fast"
TABLES = "tables"
DOCLING = "docling"
TIERS = (FAST, TABLES, DOCLING)

# How many pages the probe will open. Every page up to this many, because a table on one page in five is
# invisible to any small sample; beyond it the probe falls back to an even spread so a thousand-page ledger
# cannot make deciding cost more than doing.
PROBE_PAGES = 100


@dataclass(frozen=True)
class Decision:
    """Which tier to use, whether to OCR, and the reason - which is logged and returned in metadata.

    The reason exists because "this document took nine seconds" is a question someone will ask, and the
    honest answer is a property of the document, not a mystery of the service.
    """

    backend: str
    ocr: bool = False
    reason: str = ""


@dataclass(frozen=True)
class RoutingPolicy:
    """Thresholds for the probe, from settings (CLAUDE.md section 2: never hard-coded)."""

    min_chars_per_page: int = 120
    # Low on purpose. The two mistakes are not equal: sending a table-less document to the tables tier costs
    # a few milliseconds and reports "no tables", while missing a real table turns a policy's exclusions into
    # a stream of words and produces a confident wrong answer. Prose carries nearly no rules; a six-row
    # table carries about a dozen, and a PDF that draws its grid as one path carries fewer still.
    table_line_threshold: int = 6


@dataclass(frozen=True)
class Probe:
    """What one cheap pass over a sample of the document saw.

    `pages` is the whole document; `probed_pages` is how many of them were actually looked at. Both are
    needed, and conflating them is a real hazard: dividing a five-page sample by a fifty-page document
    reports a tenth of the true density, and a long, perfectly ordinary document then looks like a scan and
    is routed to OCR.
    """

    pages: int = 0
    probed_pages: int = 0
    chars: int = 0
    max_lines_on_a_page: int = 0
    readable: bool = True

    @property
    def chars_per_page(self) -> float:
        """Text density, averaged over the pages actually probed."""
        return self.chars / self.probed_pages if self.probed_pages else 0.0

    @property
    def table_signal(self) -> int:
        """Ruled lines on the *busiest* probed page.

        A maximum, not an average, because "does this document contain a table" is not a question about
        typical pages. A report with a table every fifth page averages almost nothing and contains ten
        tables; the page that has one is the page that answers.
        """
        return self.max_lines_on_a_page


def probe_pdf(data: bytes, pages_to_probe: int = PROBE_PAGES) -> Probe:
    """Read text length and ruled-line count from the first pages. Never raises.

    An unreadable document returns `readable=False` rather than an exception: deciding how to read something
    must not be the thing that fails. The real extractor will raise properly a moment later.
    """
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except Exception:
        return Probe(readable=False)
    try:
        page_count = len(document)
        chars = busiest = 0
        indexes = _sample(page_count, pages_to_probe)
        for index in indexes:
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    chars += textpage.count_chars()
                finally:
                    textpage.close()
                busiest = max(busiest, _ruled_lines(page))
            finally:
                page.close()
        return Probe(pages=page_count, probed_pages=len(indexes), chars=chars,
                     max_lines_on_a_page=busiest)
    except Exception:
        return Probe(readable=False)
    finally:
        document.close()


def _sample(page_count: int, cap: int) -> list[int]:
    """Page indexes to probe: every page, or an even spread once the document exceeds the cap.

    Every page, because the table question is about *presence*, not frequency. A fifty-page report with a
    table on every fifth page has forty pages of pure prose; any sample small enough to be cheap is likely
    to land on exactly those and conclude there are no tables - which is how ten tables get silently lost.

    The cap exists so the probe stays bounded on a thousand-page document, and only there does sampling
    come back. It is the lesser evil at that size: the alternative is spending seconds deciding how to spend
    seconds.
    """
    if page_count <= 0:
        return []
    if page_count <= cap:
        return list(range(page_count))
    step = page_count / cap
    return sorted({min(page_count - 1, int(position * step)) for position in range(cap)})


def _ruled_lines(page) -> int:
    """Count path objects on the page - the rules that draw a table's grid.

    A proxy, not a table detector: text pages have almost none, ruled tables have many. Getting it wrong is
    cheap in one direction (a few seconds on a document that did not need Docling) and only costs structure
    in the other, which the tier below still reports honestly as "no tables found".
    """
    import pypdfium2.raw as pdfium_raw

    try:
        return sum(1 for obj in page.get_objects(max_depth=1)
                   if obj.type == pdfium_raw.FPDF_PAGEOBJ_PATH)
    except Exception:                           # pragma: no cover - object walking is best effort
        return 0


def choose(kind: DocumentKind, data: bytes, configured: str, policy: RoutingPolicy,
           docling_available: bool = True) -> Decision:
    """Pick the extraction tier for one document.

    `configured` is DOCUMENT_EXTRACTOR: a tier name pins that tier for every document, `auto` decides here.
    `docling_available` is False when the image was built without the extra, and then the heaviest tier is
    simply not offered - a deployment that chose a small image gets less structure, never an error.

    Returns a `Decision`; raises nothing.
    """
    if configured in TIERS:
        if configured == DOCLING and not docling_available:
            return Decision(FAST, reason="docling is configured but not installed in this image")
        return Decision(configured, reason="pinned by configuration")
    if configured != AUTO:
        log_event(log, "document.unknown_extractor", configured=configured)
        return Decision(FAST, reason=f"unknown extractor '{configured}', using the safe default")

    # Only PDFs are routed. A DOCX or an XLSX is already structured; there is nothing for layout analysis
    # to recover, so the dedicated readers are both faster and more accurate.
    if kind is not DocumentKind.PDF:
        return Decision(FAST, reason=f"{kind.value} is already structured")

    probe = probe_pdf(data)
    if not probe.readable or not probe.pages:
        return Decision(FAST, reason="the document could not be probed; the reader will report why")

    if probe.chars_per_page < policy.min_chars_per_page:
        # Little or no extractable text: either a scan or a page of images. Text readers return "" here and
        # say nothing, which looks exactly like an empty document - the worst possible failure mode.
        if docling_available:
            return Decision(DOCLING, ocr=True,
                            reason=f"{probe.chars_per_page:.0f} characters per page: scanned or image-only")
        return Decision(FAST, reason="scanned or image-only, but no OCR backend is installed")

    if probe.table_signal >= policy.table_line_threshold:
        return Decision(TABLES, reason=f"{probe.table_signal} ruled lines on a probed page: tabular layout")

    return Decision(FAST, reason="digital text without tabular layout")
