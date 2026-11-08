# Document extraction — the tier ladder

How hard a document is to read is a property of *the document*, not of the deployment. agent-document
therefore probes each PDF and picks one of three tiers. Built 8 November 2026.

## 1. Why not one extractor

The three failure modes a single extractor always has:

- **A resume read by a layout model** costs seconds and a multi-gigabyte image to produce the identical
  string a text reader returns in a millisecond.
- **A policy wording read by a text reader** comes out as `Flood damage No 0 Fire damage Yes 250000`. The
  association between a clause and its limit is gone, and nothing says so. That is not a worse answer; it
  is a confident wrong one.
- **A scanned claim form read by any text reader** returns `""`. Silently. It looks exactly like an empty
  document.

## 2. The ladder

| Tier | Library | Licence | Speed | What it gets |
|---|---|---|---|---|
| `fast` | pypdfium2 | BSD-3-Clause / Apache-2.0 | ~10 ms | text, page count, metadata |
| `tables` | pdfplumber (+ pdfminer.six) | MIT | ~10–200 ms | the above **plus real table structure** |
| `docling` | Docling | MIT (models Apache-2.0) | seconds | layout-aware reading order, tables, **OCR** |

DOCX and XLSX never route: they are already structured, so `python-docx` and `openpyxl` are both faster and
more accurate than layout analysis.

## 3. How a document is routed

`DOCUMENT_EXTRACTOR=auto` (the default) probes the first 5 pages with the cheapest reader and looks at two
numbers. Naming a tier instead pins it for everything.

```
chars/page < ROUTE_MIN_CHARS_PER_PAGE (120)   ->  docling + OCR   scanned or image-only
ruled lines/page >= ROUTE_TABLE_LINE_THRESHOLD (6)  ->  tables     tabular layout
otherwise                                      ->  fast           digital text
```

`ROUTE_TABLE_LINE_THRESHOLD` is deliberately low. The two mistakes are not equal: sending a table-less page
to the tables tier costs milliseconds and reports "no tables", while missing a real table produces the
confident wrong answer above. Prose carries almost no rules; a six-row table carries about a dozen, and a
PDF that draws its grid as a single path carries fewer still.

The tier and the reason are recorded on every result (`metadata.extractionTier`,
`metadata.extractionReason`) and logged, so "why did this document take nine seconds" is answerable without
reproducing it.

**Nothing fails over a missing tier.** An image built without the Docling extra simply never offers that
tier — the router degrades and logs. A *broken document* still raises `FILE-INV-0011`: that is a real
failure the caller must see, never a plausible-looking empty result.

## 4. Measured, 8 November 2026

Three fixtures through the real service code on CPU:

| Document | `auto` picked | Result | `fast` would give | `tables` would give |
|---|---|---|---|---|
| Text resume | `fast` (0.01 s) | 20 words | same | same, 20× slower |
| Policy with a 6-row exclusions table | `tables` (0.02 s) | 23 words + **the table, rows intact** | 23 words, **0 tables** | — |
| Scanned claim form (image only) | `docling` + OCR (22 s) | **10 words read off the pixels** | **0 words** | **0 words** |

The middle row is the whole argument. `['Flood damage', 'No', '0']` survives as a row; without the tables
tier it is three words in a sentence.

OCR output is approximate — the run above produced `Policy number. POL-88123` and `Amountclaimed: 42000`.
Good enough to find and index a document, not something to quote as an exact value.

### A production-shaped document

50 pages, 120 KiB, ~4,400 characters of prose per page, a ruled table every fifth page (10 in total):

| Tier | Time | Text | Tables |
|---|---|---|---|
| `fast` | 0.12–0.17 s | 218,319 chars | 0 |
| `tables` | ~42 s | 218,319 chars | **10** |
| `docling` | ~117 s warm (**253 s** on the first call, loading models) | 227,230 chars | 10 |

`auto` routes it to `tables`, after a **168 ms** probe — 0.4% of the work it is deciding about.

**Only `fast` is interactive at this size.** `tables` at 42 s and `docling` at ~2 minutes are background work:
a bulk ingestion job with a progress handle, not a chat upload. Docling's steady-state memory was ~111 MiB
of Python allocations per document on top of the loaded models.

### The probe had two defects, both found by measuring

Worth recording, because both were invisible on small fixtures and both failed in the expensive direction:

1. **The density was divided by the wrong denominator** — characters from the sampled pages over the *whole*
   document's page count. A 50-page document reported 452 characters per page instead of 4,407. A longer one
   would have fallen under the "scanned" threshold and been sent to Docling **with OCR**: the slowest path in
   the system, chosen for an ordinary digital document.
2. **Sampling cannot detect tables.** A table every fifth page means 80% of pages are pure prose, and any
   sample small enough to be cheap lands on those. The probe now opens *every* page up to 100 (3.4 ms each)
   and takes the **busiest** page's ruled-line count, not the average — presence is not a question about
   typical pages. Beyond 100 pages it falls back to an even spread, so deciding never costs more than doing.

## 5. Licensing: why PyMuPDF left

PyMuPDF was the PDF reader in both ats-orchestrator and agent-document. [PyPI states its licence](https://pypi.org/project/pymupdf/)
as **"Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License"**. AGPL section 13 covers use over a
network, which is what this platform is, so it is not a dependency a hosted multi-tenant product carries
without buying the commercial licence. Replaced on 8 November 2026 by pypdfium2 (BSD/Apache) and pdfplumber
(MIT); Docling is MIT and hosted by LF AI & Data.

Verified: 7 extraction cases (single page, multi-page, unicode, empty, long, not-a-PDF, corrupt) produce
**identical words and page counts** before and after, and the same errors.

Two behaviour changes came with it, both deliberate:

- **PDFium returns text drawn outside the visible page.** PyMuPDF dropped it. Text hidden off-page is a
  standard way to smuggle instructions into a resume, and the injection scan can only judge what extraction
  returns — so seeing more is the safer failure mode (docs/security_llm.md).
- **PDFium reports CRLF**, normalised to LF so stored text matches every other extractor's.

PyMuPDF's table detection went with it. The `fast` tier returns no tables at all rather than guessing, which
is what the `tables` tier exists for.

## 6. Running Docling

It is an optional extra, off by default, because the models make the image multi-gigabyte
([docling-serve publishes 4.4 GB CPU / 11.4 GB CUDA](https://github.com/docling-project/docling-serve)).

```
docker build --build-arg WITH_DOCLING=1 -f services/agents/agent-document/Dockerfile .
```

The build installs the extra and bakes the models with `docling-tools models download --output-dir /models`,
setting `DOCLING_ARTIFACTS_PATH=/models`. Nothing is downloaded on a user's request, which is also what makes
the service work in an air-gapped cluster.

`DOCLING_ARTIFACTS_PATH` is deliberately **not** set in values.yaml or docker-compose. A ConfigMap entry —
even an empty one — overrides the image's own ENV and sends the service hunting for models at run time.

## 7. Open

- Only PDFs are routed. Docling reads PPTX, HTML, EPUB, images and email; `detect_kind` admits only
  PDF/DOCX/XLSX, so widening the ladder means widening the accepted content types first.
- `EXTRACT_TIMEOUT_S` (300 s) bounds one document's parse at any tier and returns `FILE-TMO-0010`. It frees
  the *caller*, not the worker thread: Python cannot interrupt a running C extension, so a pathological file
  keeps one parse slot busy until it finishes. Bounded by `PARSE_CONCURRENCY`, and far better than holding a
  request open for minutes. Verified: a 1 s timeout against a 4 s parse returned in 1.01 s.
- Measured on a 50-page document (above). **Neither `tables` nor `docling` is viable in a chat upload** at
  that size; wiring them to a durable journey with a progress handle is the next step for them.
- ats-orchestrator still parses PDFs inline instead of calling agent-document, so the two extraction paths
  can drift. Closing that is the "shared ingest capability" item in docs/impl_multi_domain.md section 8.
