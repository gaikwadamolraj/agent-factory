# ats-platform

Profile skill vs JD: a multi-tenant recruiter chat assistant that screens resumes against job descriptions for
**any industry**. Coding agents: read [CLAUDE.md](CLAUDE.md) first.

```
Browser ─► frontend (Next.js) ─/api/backend/v1/*─► orchestrator (FastAPI + LangGraph) ──MCP + service JWT──► agent (Strands)
           login, client/project,      proxy        JWT + tenant middleware, chats,                       skill v1, autonomous
           chats, PDF upload, Stop                  resumes, screenings, abort, queue                      tools, grounding, memory
                                                          │                                                     │
                     schema Job (Alembic + seed) ─► Postgres ats_platform ◄──────────────────────────────────────┘
                                                    Redis (cache, memory, abort flags, throttling)
           all services ─OTLP─► Grafana LGTM (Prometheus metrics, Loki logs, Tempo traces, "ATS Platform Overview" dashboard)
           LLM + embeddings: LM Studio (OpenAI-compatible)
```

| Path | What it is |
|---|---|
| [services/common](services/common) | Shared, versioned lib: config, logging + OTEL, error catalog, resilience, Redis keys, JWT, base models, tenant tables/repositories, middlewares |
| [services/schema](services/schema) | Database service: creates `ats_platform`, Alembic migrations, demo seed (k8s Job) |
| [services/orchestrator](services/orchestrator) | `/api/v1`: auth, chats (tenant-scoped), SSE turns, abort, LangGraph assistant |
| [services/agent](services/agent) | Strands screening agent behind MCP (`screen_candidate`, `ask_agent`, `cancel_request`) |
| [services/frontend](services/frontend) | Next.js UI |
| [deploy/k8s](deploy/k8s) | Kustomize base + overlays (namespace `ats-platform`), Grafana LGTM + dashboard |
| [docs](docs) | [access_and_onboarding.md](docs/access_and_onboarding.md) (roles, users, projects, Grafana logins, skills) and `impl_*.md` for complex flows - not shipped in images |

## Run locally

```bash
lms server start --bind 0.0.0.0 --port 1235            # LM Studio with meta-llama-3.1-8b-instruct + an embedding model
docker compose up --build
```
UI http://localhost:3000/ats (users `acme.recruiter`, `acme.admin`, `globex.recruiter`, password `Demo@12345`),
Grafana http://localhost:3001/grafana.

## Deploy to k3d

```bash
./deploy.sh                 # schema Job -> Grafana LGTM, agent, orchestrator, frontend (namespace ats-platform)
```
Details, secret keys and access: [deploy/k8s/README.md](deploy/k8s/README.md).

## Demo script

1. Sign in as `acme.recruiter`, pick the **Engineering hiring** project, press **New chat**.
2. Paste a JD, attach a resume PDF, press **Screen candidate**: the timeline shows each agent step live.
3. Ask follow-ups ("Which must-haves are missing?", "What should I ask about X?").
4. Start another screening and press **Stop**: the turn ends at once and the agent's LLM call is cancelled.
5. Open Grafana → Dashboards → ATS → **ATS Platform Overview**: active users, who did what, latency, LLM calls, aborts, errors by code.
6. Sign in as `globex.recruiter`: Acme's chats are invisible (tenant isolation).

## Error codes

`{DOMAIN}-{CATEGORY}-{NNNN}` (e.g. `WFL-CAN-0001`), defined in `ats_common.errors` and each service's `errors.py`.
Every error response: `{"error": {code, message, hint, retryable, retryAfter, requestId}}` - never a stack trace.
