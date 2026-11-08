# Platform overview

The ATS (recruiter screening assistant) is the first product built on a set of **standalone agents**. Each agent
does one job, has its own service, and is reused by any product through MCP. Product-specific logic lives in the
product's orchestrator and in **skills** (docs/impl_agent_platform.md).

## Naming convention

| Kind | Name pattern | Examples |
|---|---|---|
| Reusable agent | `agent-<what it does>` | `agent-skill`, `agent-guardrail`, `agent-knowledge-graph` |
| Product agent | `agent-<product>-<what>` | `ats-orchestrator` |
| Product app (not an agent) | `<product>-<what>` | `ats-ui`, `schema-migrate` |
| Platform service | `<what>-service` | `auth-service` |
| Shared infrastructure | component name only | `postgres`, `redis`, `falkordb`, `llm-gateway`, `lgtm` |

One name is used everywhere for a service: folder `services/<name>`, Kubernetes Deployment and Service (DNS
`http://<name>`), the audience of the service tokens it accepts, and its `SERVICE_NAME` in logs and traces. Python
packages use the same name with `_` (`agent_skill`). Names are singular.

## Services

| Service | Role | Port | Called by |
|---|---|---|---|
| `ats-ui` | Next.js UI for recruiters | 3000 | browser |
| `auth-service` | Login, refresh, logout, user management, user tokens (ES256), service tokens (client credentials), JWKS | 8004 | ats-ui (proxy), every service (JWKS once, service tokens) |
| `ats-orchestrator` | ATS API `/api/v1`: chats, documents, screenings, the LangGraph chat assistant; calls the agents | 5000 | ats-ui |
| `agent-skill` | Runs skills (Strands agent). ATS screening is its first skill | 8001 | orchestrator |
| `agent-compute` | Runs code another agent wrote, through the sandbox, and reports the result | 8002 | orchestrator |
| `agent-sandbox` | Isolated code execution: no network, no secrets, time and memory limits | 8080 | agent-compute only |
| `agent-knowledge-graph` | Knowledge graph API: store text as facts, search, answer (Graphiti on FalkorDB, one graph per tenant) | 8002 | orchestrator |
| `agent-knowledge-graph-worker` | Extraction worker: turns queued text into graph entities and facts with the LLM; scaled by KEDA | - | (queue) |
| `agent-guardrail` | Says whether untrusted text (document, chat message) tries to instruct the AI: NeMo rail + injection classifier + pattern checks | 8003 | orchestrator, knowledge graph, document agent |
| `agent-document` | Extracts text, tables and metadata from PDF / DOCX / XLSX, injection-scans the result through the guardrail, optionally chunks and embeds | 8006 | orchestrator |
| `registry-service` | Admin and authoring plane: skills and versions, approvals, IAM policy, tool catalogue, delegated admin (identity writes go to auth-service) | 8005 | admin-ui, agents (MCP) |
| `admin-ui` | Next.js admin console: skills, approvals, policies, users, clients, catalogue | 3000 | browser (`/agent-factory`) |
| `schema-migrate` | Job: creates the database, runs Alembic, seeds demo data in dev | - | Helm hook |

Shared code is in `services/common` (`agent_common`): config, logging and telemetry, errors, resilience, Redis keys,
database and tenant repositories, middlewares, the LLM client, and the clients for the knowledge graph and the
guardrail. Services never import each other.

```
browser ─► Traefik ─► ats-ui (proxy /ats/api/backend)
                          ├─► auth-service            login / refresh / users; JWKS + service tokens for services
                          └─► ats-orchestrator ──MCP──► agent-skill
                                                     ├─MCP──► agent-compute ──► agent-sandbox
                                                     ├─MCP──► agent-knowledge-graph ◄── queue ── agent-knowledge-graph-worker
                                                     ├─MCP──► agent-document ──MCP──► agent-guardrail
                                                     └─MCP──► agent-guardrail
browser ─► Traefik ─► admin-ui (/agent-factory) ─► registry-service ─► auth-service (delegated identity)
  every LLM / embedding call ──► llm-gateway (LiteLLM) ──► LM Studio / vLLM / hosted models
  every service ──OTLP──► lgtm (Grafana, Prometheus, Loki, Tempo)
```

## Namespaces

| Namespace | Holds |
|---|---|
| `agent-factory` | Agents, apps and auth-service; ExternalName aliases `postgres-service` / `redis-service` pointing to agent-infra |
| `agent-infra` | Shared infrastructure: `postgres` (pgvector), `redis` (one Redis 8 for everything), `falkordb`, `llm-gateway`, `lgtm` |
| `keda` | KEDA operator (scales the extraction worker on the episode queue) |

Each namespace has one Secret: `agent-factory-secrets` and `agent-infra-secrets`. You create them;
`./deploy.sh init` lists the keys and the chart never contains secret values.

## Security in one paragraph

Users sign in at `auth-service`, which issues short access tokens (audience `ats-api`) signed with its private
key and publishes the public keys at `/.well-known/jwks.json`. Every service verifies every token itself, in
process, with those keys (fetched once, refetched only for an unknown key id): no per-request call to auth-service.
Tenant requests must name the token's client (`X-Client-Id`) and a project the user is a member of; role and
membership come from a cached snapshot keyed by the user's authz version, and any change bumps the version, so older
tokens are refused at once (the UI refreshes silently). Services call each other with short-lived
service tokens they get from auth-service (client credentials, audience = the called service) and each service
allows only listed callers and tools. Untrusted text reaches an LLM only through `agent_common.prompt_safety` and
the guardrail check; scores and decisions are checked in code. Details: docs/security_auth.md, docs/security_llm.md.

## Deploy

| Where | How |
|---|---|
| Kubernetes (any cluster) | Helm chart `deploy/helm/agent-factory`: `./deploy.sh init` once, then `./deploy.sh` (all), `./deploy.sh <service>` (build one), `./deploy.sh apply` (no builds). See deploy/README.md |
| Laptop | `./scripts/local_env.sh` (creates `.env` with generated keys), then `docker compose up --build` |

## Old names

| Before 2026-11-05 | Now |
|---|---|
| `orchestrator` / `ats-orchestrator` | `ats-orchestrator` |
| `agent` / `ats-agent` | `agent-skill` |
| `compute` / `ats-compute` | `agent-compute` |
| `sandbox` / `ats-sandbox` | `agent-sandbox` |
| `knowledge` / `ats-knowledge` (+ `-worker`) | `agent-knowledge-graph` (+ `-worker`) |
| `ats-llm-gateway`, `ats-lgtm`, `ats-falkordb` | `llm-gateway`, `lgtm`, `falkordb` (namespace `agent-infra`) |
| `ats-knowledge-redis` / `graph-redis` | merged into `redis` (Redis 8, database 0 for the semantic cache) |
| Postgres / Redis in namespace `amol` | `postgres` / `redis` in `agent-infra` |
| kustomize `deploy/k8s` | Helm `deploy/helm/agent-factory` |
| login in the orchestrator, shared HS256 secret | `auth-service`, ES256 + JWKS |
