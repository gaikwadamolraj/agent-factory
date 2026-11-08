# Candidate onboarding & bulk matching ("ats-scan") — design

Source requirement: docs/ats-scan.md. This is the implementation design for the next ATS journey: onboard many
candidate resumes, build a knowledge graph per candidate, then match a JD against all of them fast and let the
recruiter drill in. Built on the platform pieces that come before it in the sequence (agent-document, Temporal,
workflow/artifact tables). Deploy stays on hold; this doc guides the build.

## 1. Flow

```
recruiter uploads resumes (FE sends ONE file at a time, <=10 shown, progress list)
  -> POST /api/v1/candidates/documents  (orchestrator)         [store file + row status=uploaded]
  -> Temporal workflow "onboard_candidate" per file (KEDA scales workers: 1 profile at a time)
       read (agent-document: PDF/Word/Excel -> text+tables)      status=reading
       scan (agent-guardrail: injection/safety)                  status=scanning  (blocked -> status=rejected)
       extract -> knowledge graph per candidate (agent-knowledge-graph)  status=indexing
       build candidate profile (structured summary from the KG)  status=done   (or failed + error)
  -> dashboard shows each file's stage; retry on failure; green tick + details on success

recruiter runs a JD against all candidates
  -> POST /api/v1/matches  {jd}                                 [one match run]
       for each candidate: score JD requirements vs the candidate KG + profile (match algo, mostly code)
       store a match report (artifact) per candidate                     [reuse: no re-call to answer later]
  -> "show top 5"  -> ranked candidates from the stored match run
  -> chat with one profile -> the existing chat, scoped to that candidate (answers from its KG + documents)
  -> "overview of the 5" -> assembled from the stored match reports (no recompute)
```

## 2. Data model (JSONB-heavy, to keep the schema small; all tenant-scoped, new-table conventions)

New tables (BIGSERIAL id + external_id, audit+soft-delete mixin, RLS on client_id — same as the agent-factory
tables):
- **candidates**: id, external_id, client_id, project_id, `full_name`, `email`, `current_document_id` (latest
  resume), `profile` JSONB (the built structured profile: skills, roles, years, locations, highlights),
  `profile_version`, `status`, timestamps. One row per person; **resume history** is the documents below.
- **candidate_documents**: id, external_id, tenant, candidate_id, `document_id` (FK to the existing `documents`
  table — reuse it for the file/blob/text/hash), `status` (uploaded|reading|scanning|indexing|done|failed|rejected),
  `error_code`, `stage_detail` JSONB (per-stage timings/notes for the dashboard), `workflow_id` (Temporal), ts.
  Resume history = the ordered rows per candidate; re-upload adds a row and may bump the candidate profile.
- **match_runs**: id, external_id, tenant, user_id, `jd_text`/`jd_document_id`, `jd_hash`, `skill`+version,
  `status`, `candidate_count`, timestamps. One per "run JD against candidates".
- **match_results**: id, external_id, tenant, match_run_id, candidate_id, `score`, `band`, `breakdown` JSONB
  (per-requirement met/partial/missing + evidence), `rank`. This IS the stored report reused for "top N" and the
  group overview (no recompute). Also written as an `artifact` (artifact_subjects -> candidate) so chat can cite it.
- Reuse existing: `documents` (file/blob/text/chunks/embeddings), `knowledge_episodes` (extraction queue),
  `chats`/`chat_turns` (chat with a profile), and the `workflow_runs`/`artifacts` tables from the agent platform.

Prefer JSONB for `profile`, `breakdown`, `stage_detail` so new fields don't need migrations; keep the columns that
are filtered/sorted/joined (status, score, rank, hashes, FKs, tenant) as real indexed columns.

## 3. Onboarding pipeline (Temporal)

One Temporal workflow `onboard_candidate(document_id)` per uploaded file, activities = calls to the agents:
1. `read` -> agent-document.read_document (text+tables).
2. `scan` -> agent-document.scan (or agent-guardrail): injection/safety; blocked => status rejected, stop.
3. `index` -> agent-knowledge-graph: ingest the text as an episode into the **candidate's graph** (graph id per
   candidate, namespace `talent`), wait for extraction (the worker).
4. `profile` -> build the structured `candidate.profile` from the KG (skills/roles/years/locations), set status done.
Each step updates `candidate_documents.status` + `stage_detail` (dashboard reads these). Failure => status failed +
error_code; the dashboard shows a **retry** (restart the workflow) or the error; success => green tick + details.
**KEDA scales the onboarding workers to 1 at a time** (infra/LLM limit) — one profile processed at once; the rest
queue. Temporal gives durable retries, cancel (Stop), and history per file.

## 4. Upload API (bounded, one-by-one)

- `POST /api/v1/candidates/documents` accepts ONE file (multipart), size-capped (reuse max_pdf_mb etc.), validates
  type by magic bytes, stores it (reuse documents.ingest path: blob + text + hash + chunks), creates/attaches the
  candidate (match by email/name or explicit candidate_id), inserts a `candidate_documents` row `status=uploaded`,
  starts the Temporal workflow, returns the row id so the FE tracks it.
- FE: show up to 10 files with a progress list; send sequentially so the request size limit is never crossed.
- `GET /api/v1/candidates/documents?ids=...` (or SSE) returns each file's status+stage for the dashboard.
- Idempotent per tenant+text_hash (re-upload of the same file reuses the document, like today).

## 5. Matching (fast, mostly code)

- `POST /api/v1/matches {jd}`: analyse the JD into atomic requirements once (LLM, via the screening skill's JD
  step), then for each candidate score requirements against the candidate's **KG facts + profile** — this is a
  code/graph match (embeddings + graph lookups), not a full LLM screen per candidate, so it scales to ~100.
- Store a `match_results` row + an `artifact` per candidate (subject = candidate). Scores/decisions are code-checked
  (docs/security_llm.md), never the raw LLM number.
- `GET /api/v1/matches/{id}/top?n=5` -> ranked from match_results. "Overview of the 5" -> assembled from their
  stored `breakdown` artifacts (no recompute). Reuse rule: if a match run with the same jd_hash + candidate set
  exists, return it instead of recomputing.

## 6. Chat with a profile

Reuse the existing assistant: open/extend a chat scoped to one candidate (candidate_id on the chat), grounding =
that candidate's KG facts + document chunks (the existing grounding path, filtered to the candidate's graph/docs).
"Talk to this profile" = a normal chat turn; answers cite the candidate's documents and the stored match report.

## 7. Build order (within this journey)

1. Tables (candidates, candidate_documents, match_runs, match_results) in the fresh 0001 migration.
2. Upload API + document storage + candidate attach (orchestrator), status tracking.
3. onboard_candidate Temporal workflow + activities (needs Temporal + agent-document + agent-knowledge-graph).
4. Dashboard endpoints (status per file) + FE (progress list, retry, tick/error) — in ats-ui.
5. Match run + match_results + artifacts; top-N + overview endpoints; reuse.
6. Chat-with-profile grounding scope.

## Dependencies (earlier in the master sequence)

agent-document (reader), Temporal (durable pipeline + KEDA-1), workflow/artifact tables (reuse reports), the
knowledge graph (already built). Without Temporal, the pipeline can fall back to the existing knowledge-episode
worker queue, but Temporal is the intended engine (durable retries, cancel, history, per-queue scaling).
