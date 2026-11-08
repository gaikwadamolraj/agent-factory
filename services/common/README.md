# agent-common (v0.1.0)

Shared library for every Python service. Semantic versioning: public modules stay backward compatible within a
major version. Install in a service with a uv path source:

```toml
dependencies = ["agent-common[fastapi]"]
[tool.uv.sources]
agent-common = { path = "../common", editable = false }
```
After changing the lib run `uv sync --reinstall-package agent-common` in each service.

| Module | Provides |
|---|---|
| `config` | `CommonSettings` (env only, `APP_ENV`, DB/Redis (`REDIS_DB` override)/auth/OTEL) |
| `logging`, `telemetry`, `context` | non-blocking JSON logging with request context; OTEL traces/metrics/logs |
| `errors`, `fastapi_errors` | `{DOMAIN}-{CATEGORY}-{NNNN}` catalog, `AppError`, safe error envelope |
| `resilience` | `resilient_call` (timeout, bounded retry), circuit breakers |
| `redis_keys` | registered key patterns + TTLs, `ConfigVersion` change events |
| `security` | `JwksVerifier` (ES256 tokens of the auth-service, JWKS cached, refetch on unknown kid), `verify_user_token` / `verify_service_token`, `ServiceTokenProvider` (client_credentials, cached per audience), `security.testing.LocalTokenIssuer` for tests |
| `authz` | versioned authz: `AuthzService` (authz version check, per-version role/membership snapshot), `bump_authz_version`, `bump_client_authz_version` |
| `testing` | `FakeRedis` for tests |
| `knowledge` | knowledge-service contract: wire models, ontology schema + validation, `KNW-*` errors; `knowledge.client.KnowledgeClient` (extra `mcp`) |
| `llm` | the one LLM layer: settings (ats-llm-config), alias per purpose + per-client policy, guarded gateway client, structured completions, embeddings (extra `llm`; docs/llm_gateway.md) |
| `mcp_client` | `McpToolClient`: call an internal MCP service with a service token, timeout, retry, breaker (extra `mcp`; MCP SDK 1.x and 2.x) |
| `models` | `ApiModel` / `ApiResponse` (snake_case inside, camelCase on the wire) |
| `db` | `Database`, tenant tables (clients, projects, users, members), `TenantScope`, repositories |
| `middleware` | request context, security headers, JWT auth (+ authz version), tenant headers (authz snapshot), service auth, tool policy, `secure_mcp_app` |
