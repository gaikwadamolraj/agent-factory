# CLAUDE.md - Engineering guidelines for coding agents

These rules apply to every change in this repository. Code is maintained by coding agents, so follow them exactly;
when a rule cannot be followed, say so explicitly in the change description.

Service vs lib (domain-driven): if something has its own role, data or runtime it is a **service**; if it is code
reused by several services it goes into **services/common** (the `agent-common` lib). Services never import each other.
Services are grouped by domain under `services/<group>/<service>`: `ats/` (ats-ui, ats-orchestrator), `agents/`
(agent-skill, agent-compute, agent-sandbox, agent-knowledge-graph, agent-guardrail, agent-document), `platform/` (auth-service,
schema, registry-service, admin-ui). The shared lib stays at `services/common`.

## 2. Design and code style
- DRY and SOLID. No duplicated code: reuse within a service via a module, across services via `agent_common`.
- Clear, intention-revealing names (`screen_candidate`, `membership_cache_ttl`), no abbreviations like `tmp`, `d`, `x`.
- Constants used in more than one place go to `constants.py` (service) or `agent_common.constants` (shared).
- Every function/class has a short, precise docstring (what, inputs/outputs, side effects, errors raised).
- Keep complexity and memory in check: stream large data, avoid N+1 queries, bounded caches and queues.
- Extensible by configuration: concurrency, pool sizes, timeouts, retries, batch sizes come from settings or
  parameters, never hard-coded.
- Providers behind interfaces (vector store, blob store, LLM, embeddings, document sources): pgvector today,
  another backend tomorrow without touching callers.
- LLM calls only through `agent_common.llm` and the LLM gateway, by purpose alias (`Purpose`); never a real model
  name, endpoint or provider key in a service. Models are configured in the gateway (docs/llm_gateway.md).
- Untrusted text (documents, chat messages, other sources) reaches an LLM only through `agent_common.prompt_safety`
  (`neutralize`/`fence`); anything an LLM returns that matters (scores, decisions, numbers) is checked in code
  against stored data before a user sees it. Prompts alone are not a control (docs/security_llm.md).
- Agent frameworks (LangGraph, Strands, CrewAI, ADK): framework code stays in `agents/`/`tools/`; business logic
  stays framework-free so it can be reused or tested without the framework.
- Look up current library docs with Context7 (MCP) before using an API you are not sure about.

## 3. Python, casing and API contracts
- Python 3.11, `uv` only (`uv add`, `uv lock`, `uv sync`); commit `uv.lock`; no Poetry/pip files.
- snake_case inside Python; camelCase on the wire (API, MCP tools, events). Use `agent_common.models.ApiModel`
  (requests, `extra="forbid"`) and `ApiResponse` (responses) - they convert automatically.
- APIs are versioned: `/api/v1/...`. Responses never expose table or column names, internal ids of other tenants,
  or stack traces.
- Validate every input (types, lengths, ranges, enums, file type and size) at the edge.

## 4. Configuration and environments
- All config via environment (pydantic-settings) in `config.py`, cached with `@lru_cache`; secrets only from the
  runtime environment (k8s Secret), never in code, images or commits. Provide `.env.example`, never `.env`.
- `APP_ENV` in `DEV | STAGE | UAT | PRE_PROD | PROD`. In PROD: no `/docs`/OpenAPI, no `X-Served-By`, strict CORS
  allow-list, MCP debug off.
- Values that may change at runtime (prompt versions, error texts) may be read from DB with a Redis cache; caches
  include the config version (`agent_common.redis_keys.ConfigVersion`) so a change refreshes them.

## 5. Errors
- Codes follow `{DOMAIN}-{CATEGORY}-{NNNN}`, e.g. `WFL-TMO-0002`. Codes never contain service or function names.
  - DOMAIN: AUTH, TEN (tenant), VAL, CHAT, FILE, SCR (screening, a domain pack), AGT, KNW (knowledge graph),
    PLN (planning), SCH (scheduling), WFL (workflow/turn), LLM, DB, SYS
  - CATEGORY: MIS missing, INV invalid, EXP expired, FBD forbidden, NFD not found, CNF conflict, LIM limit,
    TMO timeout, RTL rate limited, UNA unavailable, CAN cancelled, INT internal
- Shared codes: `agent_common.errors.Errors`; service codes: `<service>/errors.py` via `register(...)`.
- Raise `AppError(spec)`. Responses: `{"error": {code, message, hint, retryable, retryAfter, requestId, fields}}`.
  Messages are friendly to users and agents and say what to do next (retry, wait `retryAfter`, fix field X).
- Never return stack traces or internal exception text; log them instead.

## 6. Resilience
- Every outbound call (LLM, MCP, embeddings, HTTP, Redis-heavy loops) goes through
  `agent_common.resilience.resilient_call` with an explicit timeout, a bounded `RetryPolicy` and a named circuit
  breaker (`get_breaker`). Limits come from settings.
- Retry only idempotent/transient failures; never retry `asyncio.CancelledError`.
- Cancellation must propagate end to end: when a user aborts, the orchestrator cancels its task, the MCP call is
  cancelled, the agent cancels the LLM request (closing the HTTP stream stops generation and cost).
- Use queues for load levelling (LLM request queue, background jobs); queue sizes are configurable.

## 7. Multi-tenancy and security (zero trust)
- Tenants: `clients -> projects`; users belong to one client and are members of projects.
- Every tenant request carries `X-Client-Id` and `X-Project-Id`; `TenantMiddleware` checks them against the JWT
  client and project membership. Every tenant-owned table has `client_id` and `project_id`, and every
  query goes through `TenantScope` / `TenantRepository`.
- Only `auth-service` signs tokens (ES256; public keys at `/.well-known/jwks.json`, `kid` = RFC 7638 thumbprint,
  rotation via `auth-signing-key-previous`). Every service verifies every token locally with
  `agent_common.security.JwksVerifier` (keys cached, refetched only for an unknown `kid`); no per-request call to
  auth-service, no shared secret, never trust identity headers set by a proxy.
- User auth: access JWT `aud=ats-api` (httpOnly cookie or Bearer) with claim `authz_ver`. Role, active state and
  memberships come from `agent_common.authz` (snapshot cached per authz version; DB only on a miss). Any change to a
  user's role, state or memberships calls `bump_authz_version` (client-wide: `bump_client_authz_version`); older
  tokens get `AUTH-EXP-0002` and the UI refreshes silently. Admin endpoints need role admin in token and snapshot.
- Service-to-service: OAuth2 client_credentials at auth-service `POST /oauth/token` (`client_id` = service name,
  secret `client-secret-<service>`, `audience` = called service; allowed pairs in
  `services.authService.oauthClients`). Callers use `ServiceTokenProvider` (cached to ~80% of the 5-minute
  lifetime); the called service checks `aud` = its name, `ALLOWED_CALLERS` and the tool allow-list
  (`ToolPolicyMiddleware`). MCP servers wrap their app with `agent_common.middleware.secure_mcp_app`.
- Middleware order (outermost first): request context -> security headers -> CORS -> auth -> tenant/tool policy.
  Public paths are explicit allow-lists (`PathRules`).
- Brute-force protection on login and on client_credentials; bcrypt password hashes; no secrets or tokens in logs.
  Details: docs/security_auth.md.

## 8. Data
- SQLModel for tables and CRUD; repositories for all DB access (no raw SQL in handlers). Shared tenant tables and
  repositories live in `agent_common.db`.
- One connection mechanism: `agent_common.db.Database` (pooling, statement timeout, application_name).
- Schema changes only through Alembic in `services/schema` (one version table), reviewed migrations, indexes for
  every foreign key and every query filter (tenant columns first in composite indexes).
- Seed/demo data: `services/schema/seed/*.sql`, idempotent (`ON CONFLICT DO NOTHING`).
- Redis keys only via `agent_common.redis_keys.build_key(...)`; each key has a registered pattern, TTL, purpose and owner.
  One Redis (maxmemory-policy `volatile-lru`): cache-like keys get a TTL (evictable); keys that must never vanish
  (version counters) have none. Vector search (RedisVL) only works in database 0 (`REDIS_DB=0`).

## 9. Logging and telemetry
- Use `agent_common.logging.get_logger(__name__)` and `log_event(log, "domain.action", field=...)`; never `print`.
- Logging is non-blocking (queue + background listener), JSON, and carries request_id, user_id, client_id,
  project_id automatically. Never log passwords, tokens, resume or JD text.
- OpenTelemetry (`agent_common.telemetry.setup_observability`) exports traces, metrics and logs over OTLP to Grafana
  LGTM. Add business metrics with clear names (`agent_turns_total`, `agent_screening_duration_seconds`, ...) and
  low-cardinality labels (client, project, outcome - never free text).

## 10. Prompts and skills
- Prompts and agent skills are versioned modules; released versions are never edited - add `vN+1` and switch by
  config. They may later be served from DB with cache; keep the loader interface stable.
- Prompts are domain-agnostic: no hard-coded industries or technologies.

## 11. Tests
- Unit tests for every function with real assertions on behaviour (no `assert True`, no tests that only mock the
  thing under test). Integration tests for flows (DB, MCP, agent graph) under `tests/integration`.
- A change is done only when its tests pass; report failures honestly with output.

## 12. Containers and deployment
- Multi-stage images, slim runtime, non-root, no build tools, `.dockerignore` excludes `.env*`, `docs/`, tests,
  `impl_*.md`.
- Kubernetes: one Helm chart `deploy/helm/agent-factory`, deployed only with `./deploy.sh` (release `ats`).
  Namespaces `agent-factory` (ATS services, release namespace) and `agent-infra` (shared infra: postgres, redis,
  falkordb, llm-gateway, lgtm; one Redis 8 for everything); both are Namespace objects in the chart, kept on uninstall.
  Infra stays per-component in `templates/infra/<component>/`. Application backends are NOT per-service files: they
  are values blocks (`services:` public-capable, `agents:` internal) rendered by the generic
  `templates/services/backend.yaml` (Deployment + Service + NetworkPolicy + PVCs) and `templates/services/config.yaml`
  (ConfigMaps); routing is the generic `templates/routing/`. Add a backend by copying a values block - no new
  template. values in `values.yaml` + `values-<env>.yaml`.
  Config (ConfigMap) in its own template; Deployment + Service rendered together; every object sets its namespace;
  apps reach infra by FQDN. A backend is reachable from outside ONLY if its block declares a `route` (Traefik
  IngressRoute); expose nothing by default. The schema migration is a hook Job (`post-install,pre-upgrade`). YAML in
  block style only (no `{}`/`[]` flow mappings). Never change routes of other namespaces; new routes for shared hosts
  go to review files (`deploy/review/`).
- Secrets come from `agent-factory-secrets` (agent-factory) and `agent-infra-secrets` (agent-infra), created by hand,
  never committed and never rendered by the chart.

## 13. Documentation
- Each service has a README.md. Complex logic gets `docs/impl_{name}.md` (excluded from builds/images).
- Update docs in the same change as the code.

# Output & Execution Style (Codex Mode)
- **Silence Intermediate Narration:** Do NOT explain what you are about to do before calling tools, and do NOT narrate individual file reads or minor edits. Execute all necessary file inspections and edits silently in batches.
- **Plan First:** For any multi-file change, present a concise bulleted **Execution Plan** (target files, architectural changes, and verification command) and wait for confirmation before editing.
- **Structured Final Summary Only:** After completing edits and running tests, output ONLY a clean summary structured as:
  1. **Summary of Changes** (1–2 sentences)
  2. **Modified / Created Files** (bulleted list with 1-line purpose per file)
  3. **Verification** (exact command run and pass/fail status)
