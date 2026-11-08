# ats-orchestrator

ATS orchestrator (`ats_orchestrator`): FastAPI `/api/v1` + LangGraph conversational assistant.
Sign-in and user management (`/api/v1/auth/*`, `/api/v1/admin/*`) moved to [auth-service](../auth-service).
Every request needs an access token (verified locally with the auth-service JWKS, plus the authz version check);
calls to the agents use auth-service service tokens (`SERVICE_CLIENT_SECRET`, client_credentials).

| Endpoint | Notes |
|---|---|
| `POST/GET /api/v1/chats` | tenant headers `X-Client-Id`, `X-Project-Id` required |
| `GET/PATCH/DELETE /api/v1/chats/{chatId}` | detail (+ `runningTurnId`), rename / set `jobDescription`, delete |
| `POST /api/v1/chats/{chatId}/messages` | multipart `content`, `action` (chat/screen), `jobDescription?`, `file?` (PDF) -> SSE |
| `POST /api/v1/chats/{chatId}/turns/{turnId}/abort` | stop a running turn down to the LLM |

SSE events: `turn_started`, `user_message`, `progress`, `heartbeat`, then one of `message` / `aborted` / `error`.
Middleware order and tenancy: [docs/impl_tenancy_auth.md](../../docs/impl_tenancy_auth.md). Abort: [docs/impl_abort.md](../../docs/impl_abort.md).

Candidate knowledge graph (`src/ats_orchestrator/knowledge/`, on when `KNOWLEDGE_MCP_URL` is set): a resume upload is
queued for the shared knowledge service in the background, screening outcomes follow, and the assistant gets the
`search_candidate_knowledge` tool. Guidance is versioned in `knowledge/guidance.py`. Details:
[docs/impl_knowledge_graph.md](../../docs/impl_knowledge_graph.md).

Local: `uv sync && PYTHONPATH=src uv run uvicorn ats_orchestrator.main:app --port 5000` (env: `.env.example`).

## Durable journeys (Temporal)

One image, one API and one worker entrypoint. The API is `ats_orchestrator.main:app`; the worker is
`python -m ats_orchestrator.worker`, and `WORKER_QUEUE` decides which queue it polls:

- `onboarding` → **J1 `onboard_subject`**: parse -> injection scan -> subject record -> index -> knowledge graph.
- `bulk-match` → **J3 `bulk_match`**: one job description against many candidates, graded, ranked, shortlisted
  ([docs/impl_bulk_match.md](../../../docs/impl_bulk_match.md)).

One Deployment per queue, each scaled by KEDA on its own backlog. The split is the point: a thousand-candidate
bulk run must never delay one recruiter's upload, and a shared queue with priority ordering would not prevent
that — it still shares workers. The API scales on request rate, each worker on its queue's backlog
(`keda.scaledObjects.atsOrchestratorOnboarding` and `.atsOrchestratorBulkMatch`).

- `workflows/onboard_subject.py`, `workflows/bulk_match.py` — the workflows. **Deterministic and
  import-light**: the Temporal sandbox re-imports them on every worker start, so they hold no clients,
  settings or network calls.
- `workflows/activities.py`, `workflows/bulk_activities.py` — the work, as plain async methods, testable
  without Temporal.
- `workflows/adapters.py`, `workflows/bulk_adapters.py` — the only ATS-specific files: they map the
  activities' domain-neutral interfaces onto this service's documents, guardrail, knowledge graph and
  screening agent. A different domain supplies different adapters and changes nothing else.
- `journeys.py` — the producers: who starts a durable run and under what business key. J1's is the resume
  upload. A chat turn still stores and indexes the document itself (the turn must be able to search what it
  just received); what the producer hands over is everything about the *candidate*, which has to outlive the
  turn. Keyed on the resume, so a re-upload reattaches to the one run. J3's is `start_bulk_match`, keyed on
  the job description — and it has **no** in-process fallback, because grading a thousand candidates inside a
  request is not a degraded version of the journey.
- `workflow_runs` is the business record of a run (`agent_common.workflow.runs` / `.projection`); Temporal keeps
  the execution history. Both are needed - see [docs/impl_temporal.md](../../../docs/impl_temporal.md) §3.1.
- `api/v1/runs.py` — `GET /api/v1/runs`, `GET /api/v1/runs/{runId}`, `POST /api/v1/runs/{runId}/cancel`. The
  reads answer from the projection and never call the engine; cancel does, and refuses when there is none.

Without `TEMPORAL_ADDRESS` there is no engine: the API keeps its in-process path and the worker refuses to
start rather than idling. The same is true when the engine is configured but unreachable - the producer
degrades instead of failing the upload, and the `temporal` circuit breaker stops every later upload paying
the connect timeout again. A local engine:
`docker run -d -p 7233:7233 temporalio/temporal:latest server start-dev --ip 0.0.0.0`.

The worker has **its own** service identity (`ats-orchestrator-onboarding`) and a narrower grant than the API:
guardrail, knowledge-graph *ingest only*, and the registry. It never gets a token for the screening agent or
the code sandbox.
