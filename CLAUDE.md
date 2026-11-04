# CLAUDE.md - Engineering guidelines for coding agents

These rules apply to every change in this repository. Code is maintained by coding agents, so follow them exactly;
when a rule cannot be followed, say so explicitly in the change description.

Service vs lib (domain-driven): if something has its own role, data or runtime it is a **service**; if it is code
reused by several services it goes into **services/common** (the `ats-common` lib). Services never import each other.

## 2. Design and code style
- DRY and SOLID. No duplicated code: reuse within a service via a module, across services via `ats_common`.
- Clear, intention-revealing names (`screen_candidate`, `membership_cache_ttl`), no abbreviations like `tmp`, `d`, `x`.
- Constants used in more than one place go to `constants.py` (service) or `ats_common.constants` (shared).
- Every function/class has a short, precise docstring (what, inputs/outputs, side effects, errors raised).
- Keep complexity and memory in check: stream large data, avoid N+1 queries, bounded caches and queues.
- Extensible by configuration: concurrency, pool sizes, timeouts, retries, batch sizes come from settings or
  parameters, never hard-coded.
- Providers behind interfaces (vector store, blob store, LLM, embeddings, document sources): pgvector today,
  another backend tomorrow without touching callers.
- LLM calls only through `ats_common.llm` and the LLM gateway, by purpose alias (`Purpose`); never a real model
  name, endpoint or provider key in a service. Models are configured in the gateway (docs/llm_gateway.md).
- Untrusted text (documents, chat messages, other sources) reaches an LLM only through `ats_common.prompt_safety`
  (`neutralize`/`fence`); anything an LLM returns that matters (scores, decisions, numbers) is checked in code
  against stored data before a user sees it. Prompts alone are not a control (docs/security_llm.md).
- Agent frameworks (LangGraph, Strands, CrewAI, ADK): framework code stays in `agents/`/`tools/`; business logic
  stays framework-free so it can be reused or tested without the framework.
- Look up current library docs with Context7 (MCP) before using an API you are not sure about.

## 3. Python, casing and API contracts
- Python 3.11, `uv` only (`uv add`, `uv lock`, `uv sync`); commit `uv.lock`; no Poetry/pip files.
- snake_case inside Python; camelCase on the wire (API, MCP tools, events). Use `ats_common.models.ApiModel`
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
  include the config version (`ats_common.redis_keys.ConfigVersion`) so a change refreshes them.

## 5. Errors
- Codes follow `{DOMAIN}-{CATEGORY}-{NNNN}`, e.g. `WFL-TMO-0002`. Codes never contain service or function names.
  - DOMAIN: AUTH, TEN (tenant), VAL, CHAT, FILE, SCR (screening), AGT, KNW (knowledge graph), WFL (workflow/turn),
    LLM, DB, SYS
  - CATEGORY: MIS missing, INV invalid, EXP expired, FBD forbidden, NFD not found, CNF conflict, LIM limit,
    TMO timeout, RTL rate limited, UNA unavailable, CAN cancelled, INT internal
- Shared codes: `ats_common.errors.Errors`; service codes: `<service>/errors.py` via `register(...)`.
- Raise `AppError(spec)`. Responses: `{"error": {code, message, hint, retryable, retryAfter, requestId, fields}}`.
  Messages are friendly to users and agents and say what to do next (retry, wait `retryAfter`, fix field X).
- Never return stack traces or internal exception text; log them instead.

## 6. Resilience
- Every outbound call (LLM, MCP, embeddings, HTTP, Redis-heavy loops) goes through
  `ats_common.resilience.resilient_call` with an explicit timeout, a bounded `RetryPolicy` and a named circuit
  breaker (`get_breaker`). Limits come from settings.
- Retry only idempotent/transient failures; never retry `asyncio.CancelledError`.
- Cancellation must propagate end to end: when a user aborts, the orchestrator cancels its task, the MCP call is
  cancelled, the agent cancels the LLM request (closing the HTTP stream stops generation and cost).
- Use queues for load levelling (LLM request queue, background jobs); queue sizes are configurable.

## 7. Multi-tenancy and security (zero trust)
- Tenants: `clients -> projects`; users belong to one client and are members of projects.
- Every tenant request carries `X-Client-Id` and `X-Project-Id`; `TenantMiddleware` checks them against the JWT
  client and project membership (cached). Every tenant-owned table has `client_id` and `project_id`, and every
  query goes through `TenantScope` / `TenantRepository`.
- User auth: JWT (`aud=ats-api`). Service-to-service: short-lived service JWT (`aud=ats-agent`), verified on every
  call, plus a tool allow-list (`ToolPolicyMiddleware`).
- Middleware order (outermost first): request context -> security headers -> CORS -> auth -> tenant/tool policy.
  Public paths are explicit allow-lists (`PathRules`).
- Brute-force protection on login; bcrypt password hashes; no secrets or tokens in logs.

## 8. Data
- SQLModel for tables and CRUD; repositories for all DB access (no raw SQL in handlers). Shared tenant tables and
  repositories live in `ats_common.db`.
- One connection mechanism: `ats_common.db.Database` (pooling, statement timeout, application_name).
- Schema changes only through Alembic in `services/schema` (one version table), reviewed migrations, indexes for
  every foreign key and every query filter (tenant columns first in composite indexes).
- Seed/demo data: `services/schema/seed/*.sql`, idempotent (`ON CONFLICT DO NOTHING`).
- Redis keys only via `ats_common.redis_keys.build_key(...)`; each key has a registered pattern, TTL, purpose and owner.

## 9. Logging and telemetry
- Use `ats_common.logging.get_logger(__name__)` and `log_event(log, "domain.action", field=...)`; never `print`.
- Logging is non-blocking (queue + background listener), JSON, and carries request_id, user_id, client_id,
  project_id automatically. Never log passwords, tokens, resume or JD text.
- OpenTelemetry (`ats_common.telemetry.setup_observability`) exports traces, metrics and logs over OTLP to Grafana
  LGTM. Add business metrics with clear names (`ats_turns_total`, `ats_screening_duration_seconds`, ...) and
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
- Kubernetes: namespace `ats-platform`; kustomize `base/` + `overlays/<env>/`; files in `.yml`, block style only
  (no `{}`/`[]` flow mappings); config (ConfigMap) in its own file; Deployment + Service in one file; the schema
  migration runs as a Job. Never change routes of other namespaces; new routes for shared hosts go to review files.
- Secrets come from `ats-platform-secrets` (never committed).

## 13. Documentation
- Each service has a README.md. Complex logic gets `docs/impl_{name}.md` (excluded from builds/images).
- Update docs in the same change as the code.
