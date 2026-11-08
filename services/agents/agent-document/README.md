# agent-document

A reusable internal **document-reader agent**. Given an uploaded file (PDF, Word `.docx`, Excel `.xlsx`) it
extracts **text + tables + metadata**, can run a **prompt-injection safety scan** through the shared guardrails
service, and can **chunk + embed** the text for search. It is exposed only over **MCP** (zero-trust service tokens);
there are no public HTTP routes.

It is domain-agnostic: the ATS app uses it for resumes/JDs, and the candidate-onboarding pipeline can use it for any
uploaded document, without either importing the other.

## Tools (MCP, camelCase on the wire)

### `read_document(request)`
Extract a document. The format is detected from the **file bytes** (magic bytes), never the name or declared type.

- **Input**: `{ filename, contentType, dataBase64, scan? }`
  - `dataBase64` - the file, base64-encoded (bounded by `MAX_FILE_BYTES`, default 10 MiB).
  - `scan` - when `true`, also injection-scan the extracted text and return `safety`.
- **Output**: `{ kind (pdf|docx|xlsx), text, pages?, sheets?, tables[], metadata, truncated, safety? }`
  - `tables[]` = `{ name, rows (row-major cells), truncated }`. For XLSX one table = one sheet; for DOCX/PDF one
    table = one detected table.
  - `pages` is set for PDF, `sheets` (sheet names) for XLSX.
  - `truncated` = the text was cut at `MAX_TEXT_CHARS`.

### `scan_document(request)`
Injection-scan a block of text through the shared guardrails (useful for already-extracted text).

- **Input**: `{ text, source? }` - `source` is one of `document` (default), `resume`, `job_description`,
  `chat_message`, `other`.
- **Output**: `{ blocked, score, reasons[], detector }`.

### `chunk_document(request)`
Split text into overlapping chunks and (optionally) embed them for search.

- **Input**: `{ text, chunkSize?, chunkOverlap?, embed? (default true), includeVectors? (default false) }`
- **Output**: `{ chunks[], count, embedded, embeddingId?, embeddingDim?, vectors? }`
- Embedding is **best effort**: if the LLM gateway is unavailable the chunks are still returned with
  `embedded: false`.

## Libraries

PDFs are read at one of three tiers, chosen **per document** by a cheap probe (`DOCUMENT_EXTRACTOR=auto`),
because how hard a document is to read is a property of the document — see
[docs/impl_docling.md](../../../docs/impl_docling.md).

| Format | Library | Licence | Why |
|--------|---------|---------|-----|
| PDF `fast` | **pypdfium2** | BSD-3-Clause / Apache-2.0 | Text, pages, metadata in milliseconds. No tables, no OCR. The right tier for a resume. |
| PDF `tables` | **pdfplumber** | MIT | Real ruled-table structure for born-digital PDFs, without Docling's models. |
| PDF `docling` | **Docling** (optional extra) | MIT | Layout-aware reading order, tables and OCR for scanned pages. Multi-GB image, so off unless built with `--build-arg WITH_DOCLING=1`. |
| DOCX   | **python-docx** | MIT | Paragraphs + tables + core properties. Already structured, so never routed. |
| XLSX   | **openpyxl** (read-only, data-only) | MIT | Per-sheet cell values; streams large books. |

PyMuPDF used to read PDFs here and in ats-orchestrator. It is dual-licensed AGPL-3.0 / Artifex commercial and
was replaced on 8 November 2026; the reasoning and the before/after parity check are in that doc.

The safety scan reuses `agent_common.guardrails` (so a guardrails outage degrades to local pattern checks instead of
failing the read), and embeddings reuse `agent_common.llm.embed_documents` (one model/identity/dimension for the whole
platform). Business logic (detection, extractors, chunking, the service) is framework-free; only `server.py` knows
about MCP.

## Configuration

All via environment (`config.py`, pydantic-settings). See `.env.example`. Key settings: `MAX_FILE_BYTES`,
`MAX_TEXT_CHARS`, `CHUNK_SIZE`/`CHUNK_OVERLAP`, `ALLOWED_CONTENT_TYPES`, `PARSE_CONCURRENCY`, `GUARDRAIL_MCP_URL`,
`ALLOWED_CALLERS`, `TOOL_POLICY`.

- **Port**: `8006`
- **JWT audience**: `agent-document`
- **Default caller**: `ats-orchestrator`

## Run locally

```bash
uv sync
cp .env.example .env            # fill in what you need
uv run uvicorn agent_document.server:app --host 0.0.0.0 --port 8006
# health: curl http://localhost:8006/healthz
```

With no `GUARDRAIL_MCP_URL` set, scanning falls back to local pattern checks and no service token is needed.
