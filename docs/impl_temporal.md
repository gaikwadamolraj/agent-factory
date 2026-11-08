# Temporal — durable workflow engine (design)

Temporal is the platform's durable orchestrator: it runs multi-step work (skill workflows, candidate onboarding,
knowledge extraction) with real pause/resume, retries, timeouts, cancellation and a full run history — not just pod
retries. It is the engine the skill platform (docs/impl_agent_platform.md) and the ats-scan pipeline
(docs/impl_ats_scan.md) build on. Deploy stays on hold; this guides the build.

## 1. Topology

- **Temporal server + Web UI** in namespace `agent-infra` (shared infra), from the official Temporal images (pin a
  version). Toggleable `infra.temporal.enabled`, default off, like the other infra components.
- **Persistence = our Postgres** (`postgres.agent-infra`), two extra databases created by the schema job or an init
  step: `temporal` and `temporal_visibility`. No new datastore.
- **Web UI** exposed under `/temporal` on the shared host (gateway IngressRoute in agent-infra, admin-protected like
  `/agent-factory`: IP allow-list + basic-auth + HTTPS — it shows run internals).
- **Workers** run inside the services that own the work (e.g. the onboarding worker, the skill-run worker), each
  polling its **own task queue**; **KEDA scales each worker on its task-queue backlog** (the candidate pipeline is
  pinned to 1 at a time by its queue's max).

## 2. Shared client: `agent_common.workflow`

A thin, framework-light wrapper so services start/signal/query workflows without scattering Temporal SDK code
(business logic stays framework-free, CLAUDE.md §2):
- `WorkflowClient` (connect from `TEMPORAL_ADDRESS`, namespace per env) with `start`, `signal` (cancel = a signal),
  `result`, `describe`.
- A worker runner helper (register workflows + activities, task queue, concurrency from settings).
- Config in `CommonSettings`: `temporal_address`, `temporal_namespace`, task-queue names, worker concurrency — env,
  `@lru_cache`.
- Activities are plain async functions that call the agents via the existing MCP clients and go through
  `agent_common.resilience` (timeout + breaker). Cancellation propagates (Temporal cancel -> activity cancel -> MCP
  cancel -> LLM stream close), matching CLAUDE.md §6.

## 3. What moves onto Temporal

| Work | Workflow | Task queue (KEDA-scaled) |
|---|---|---|
| Subject onboarding (read -> scan -> subject -> index -> graph) | `onboard_subject` | `onboarding` (max 3) |
| Skill run (llm/tool/code/guard steps) | `run_skill` | `skill-run` |
| Knowledge extraction (optional: replace the custom lease worker) | `extract_episode` | `extraction` |

`workflow_runs` (business record) links to the Temporal workflow id/run id; `workflow_steps` is NOT needed —
Temporal's history is the step record. `artifacts`/`artifact_subjects` store step outputs for reuse (answer questions
without re-running), per docs/impl_agent_platform.md.

### 3.1 Who writes `workflow_runs`

Two writers, and the split is deliberate:

- **`JourneyLauncher`** (`agent_common.workflow.runs`) writes the row on the way **in**, only after the engine has
  accepted the run, so a row never claims work that does not exist. It is also the idempotency gate: it reads
  `(client_id, idempotency_key)` first and reattaches instead of starting a second execution. A concurrent launch
  loses the unique constraint and reads the winner's row.
- **`RunProjection.record_run_status`** (`agent_common.workflow.projection`) writes it on the way **out**, as an
  activity the workflow itself calls: `running` when a worker picks it up, then `completed` or `failed` with a small
  summary and the platform error code the failing activity reported.
- **Cancellation is written by the canceller**, not the run: a cancelled workflow may not start further activities,
  so `JourneyLauncher.cancel` cancels in the engine and records the transition itself.

The projection never fails the business run. If it cannot be written after its retries, the dashboard row falls
behind while the run and its real output are unaffected; `WorkflowClient.describe` still reads the engine's truth.
The alternative — failing a finished screening because its status row would not save — destroys real work to
protect a display.

### 3.2 Who starts a journey (producers)

A producer decides *when* durable work is worth starting and what business key identifies it. They live in
one file per service (`ats_orchestrator/journeys.py`) rather than in the workflow package, so the workflow
stays a description of the work and a second domain reuses it by writing its own producer.

J1's producer is the resume upload. A chat turn stores and indexes the document itself, because the turn
must be able to search what it just received; everything after that is about the *candidate* rather than the
turn, and has to outlive both. That is the part the producer hands over:

* **Business key**: `ats:candidate:document:<id>` — the resume identifies the candidate until a pack
  extracts something better, and it is the same ref the knowledge graph uses as its `subject_ref`, so the
  `subjects` row and the graph node describe one person. Re-uploading the same file, a retried request and
  two users submitting at once all reattach to one run.
* **It degrades, always.** No engine configured, engine unreachable, engine refusing the run - the producer
  returns `None` and the caller runs the in-process background ingest it used before. An upload must never
  fail because the orchestrator is down, and a reattach to a finished run is answered from `workflow_runs`
  without contacting the engine at all.
* **It is a request-path call**, so `WorkflowClient` now puts every short engine RPC (connect, start,
  signal, cancel, query, describe) behind an explicit timeout and the shared `temporal` circuit breaker
  (`temporal_connect_timeout_s`, `temporal_call_timeout_s`). After repeated failures uploads stop waiting
  for the timeout and degrade immediately. `result` is excluded: it waits for the business work.

One consequence worth knowing: the upload path and the journey would otherwise both embed the same
document. `DocumentAccess.index` therefore skips a document that is already indexed at the embedding
identity in use today, and still re-indexes after a model change or an upload whose embeddings failed.

## 4. Build order

1. Infra: Temporal server + UI in `templates/infra/temporal/` (toggleable); the two Postgres databases; the
   `/temporal` admin-protected route. **DONE 7 Nov** — `deployment.yaml` (server + UI), `network-policy.yaml`
   (named callers only, not "any pod in the namespace"), `route.yaml` (reuses
   `infra.routing.adminProtection`: IP allow-list + basic auth, because the UI shows run arguments and
   results across every tenant). Default OFF: 9 objects when enabled, 0 when not.
2. `agent_common.workflow` client + worker runner + settings. **DONE 7 Nov**, verified against a real server
   rather than deferred — see §5.
3. The `onboard_subject` journey (J1) + its worker + the `workflow_runs` projection. **DONE 8 Nov** —
   `ats_orchestrator.workflows` (workflow, activities, adapters), `ats_orchestrator.worker`
   (`python -m ats_orchestrator.worker`), migration `0003_workflow_runs`, the `atsOrchestratorOnboarding`
   values block and its KEDA ScaledObject. Verified against a real engine and a real Postgres: see §6.
   The producer is `ats_orchestrator.journeys.start_candidate_onboarding`, called from the resume upload
   path — see §3.2.
4. Port skill execution onto `run_skill`; wire `workflow_runs`/`artifacts` reuse.
5. (Optional) migrate knowledge extraction off the custom queue onto Temporal.

## 5. Validation notes

**Superseded 7 November 2026: most of this is verifiable locally with Docker, and doing so caught three real
bugs that would otherwise have surfaced on the cluster.** Do not defer Temporal work again.

* **The engine**: `docker run -d -p 7233:7233 temporalio/temporal:latest server start-dev --ip 0.0.0.0` gives
  a real in-memory server in about five seconds. Use it for every workflow change.
* **Persistence**: `temporalio/auto-setup:1.26.2` against our `pgvector/pgvector:pg16` image creates the
  `temporal` and `temporal_visibility` databases **and** their schemas itself (36 and 3 tables), reaching
  `SERVING`. Our schema job does not need to create them.
* **Bind address**: the server listens on the **pod IP**, not `0.0.0.0` or `localhost`. A `tcpSocket` probe
  works; anything inside the pod dialling `localhost:7233` does not.
* **The workflow sandbox re-imports the defining module** on every worker start, so workflow definitions must
  live in their own module importing only deterministic things — never in `__main__`, never beside the client
  or worker wiring. Getting this wrong fails with `Failed validating workflow <name>`.
* **`WorkflowAlreadyStartedError` is in `temporalio.exceptions`**, not `temporalio.client` (SDK 1.35). Re-check
  on an SDK upgrade: a wrong import inside an `except` clause turns a duplicate start into a hang.

Only KEDA task-queue scaling and the `/temporal` route remain genuine deploy-time checks.

## 6. What J1 proved (8 November 2026)

Run locally against `temporalio/temporal:latest server start-dev` and `pgvector/pgvector:pg16`, with the real
adapters (`ats_orchestrator.workflows.adapters`) rather than stubs. Both containers removed afterwards.

| Check | Result |
|---|---|
| Launch writes the row before any worker picks it up | `status=queued`, no `started_at` |
| The run reports itself | `running` -> `completed`, `started_at` and `completed_at` set |
| The summary is small and useful | `{subjectRef, stage, chunks, flagged, episodeId, notes}` |
| The run links to what it produced | `subject_id` set from the upsert activity |
| Re-submitting the same business key | reattaches: same run external id, **one** `workflow_runs` row |
| A document the project cannot read | `status=failed`, `error_code=FILE-NFD-0001`, **one attempt** (non-retryable) |
| Cancelling a queued run | `status=cancelled`, `completed_at` set, engine run cancelled |

And the producer itself (`journeys.start_candidate_onboarding`, same setup, 8 November):

| Check | Result |
|---|---|
| No engine configured | returns `None`; the caller keeps its in-process path |
| Engine configured but unreachable | returns `None` after one bounded attempt; the upload still succeeds |
| A resume uploaded | run row with `journey=onboard_subject`, key `ats:candidate:document:<id>` |
| The subject it creates | one row, `ats/candidate/document:<id>`, display name from the file, linked to the run |
| The same resume again | reattaches to the one run, answered from the database without calling the engine |
| A document the upload already indexed | onboarding reports the chunk count and does **not** re-embed |

And the read API (`/api/v1/runs`, real Postgres, handlers called directly):

| Check | Result |
|---|---|
| Listing a project's runs | only that project's; another project's and another client's absent |
| `journey` / `status` filters | applied; an unknown status is `VAL-INV-0001`, not an empty list |
| One run by id | whole row, camelCase, **no** engine or tenant identifiers in the payload |
| Another project's or client's id | `WFL-NFD-0002` (not found, never forbidden) |
| A malformed id | `WFL-NFD-0002`, not a 500 |
| Cancelling a finished run | `WFL-CNF-0002` |
| Cancelling with no engine configured | `SYS-UNA-0001`, rather than reporting a cancellation that did not happen |

The failure case is the one worth keeping: before `parse_document` was made to raise, a missing or
other-tenant document produced a *successful* run that published an empty subject. Fail-closed here is not a
nicety - a silent empty onboarding looks exactly like a real one in every list the product shows.

### Scaling signal

KEDA scales the onboarding worker on `SELECT COUNT(*) FROM workflow_runs WHERE task_queue = 'onboarding' AND
status IN ('queued','running')` - our own projection, not Temporal. So KEDA needs no engine credentials and no
extra scaler, and the autoscaling signal is the same table the dashboard reads. The cost is that a run whose
projection write failed can hold a replica up until it is reconciled; that is bounded by `maxReplicaCount` and
visible in the same place.

### 3.3 Reading a run back

`GET /api/v1/runs`, `GET /api/v1/runs/{runId}` and `POST /api/v1/runs/{runId}/cancel` (ats-orchestrator,
tenant-scoped like `/chats`). The two reads never touch the engine - that is the whole point of the
projection, and going to Temporal for a status would quietly undo it. Cancel does, because stopping work
needs it, and it refuses rather than pretending when no engine is configured.

A run is addressed by its `external_id`, never by a row id or a Temporal workflow id, and the lookup is
scoped in the query: another project's run is *not found*, not forbidden, so a guessed id tells no one that
it exists. A malformed id is the same 404 rather than a 500. The response carries the projection only -
status, `inputRef`, `resultSummary`, the platform error code - and no engine identifiers.

In the UI, the launch id reaches the chat message attachment as `onboardingRunId` and
`components/OnboardingStatus.tsx` shows one line under the uploaded resume: queued, running, onboarded (with
the passage count and whether the graph took it), failed with its platform error code, or cancelled. It
polls rather than streams, because a run outlives the turn and the page, and it stops at a final state or
after a bounded time instead of polling a stuck run forever. A flagged source is shown there too - the scan
records it rather than dropping the document, so the person reading the candidate should see it.

## 7. Open

- Nothing lists runs across a project yet (`GET /api/v1/runs` and `api.listRuns` exist, unused): the only
  view is per resume, inside the chat that uploaded it.
- `run_skill` (step 4) and the artifacts reuse it depends on.
- The `/temporal` route and KEDA behaviour under real load remain genuine deploy-time checks.
