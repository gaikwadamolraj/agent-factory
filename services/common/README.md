# ats-common (v0.1.0)

Shared library for every Python service. Semantic versioning: public modules stay backward compatible within a
major version. Install in a service with a uv path source:

```toml
dependencies = ["ats-common[fastapi]"]
[tool.uv.sources]
ats-common = { path = "../common", editable = false }
```
After changing the lib run `uv sync --reinstall-package ats-common` in each service.

| Module | Provides |
|---|---|
| `config` | `CommonSettings` (env only, `APP_ENV`, DB/Redis/JWT/OTEL) |
| `logging`, `telemetry`, `context` | non-blocking JSON logging with request context; OTEL traces/metrics/logs |
| `errors`, `fastapi_errors` | `{DOMAIN}-{CATEGORY}-{NNNN}` catalog, `AppError`, safe error envelope |
| `resilience` | `resilient_call` (timeout, bounded retry), circuit breakers |
| `redis_keys` | registered key patterns + TTLs, `ConfigVersion` change events |
| `security` | bcrypt, user JWT (`aud=ats-api`), service JWT (`aud=ats-agent`) |
| `models` | `ApiModel` / `ApiResponse` (snake_case inside, camelCase on the wire) |
| `db` | `Database`, tenant tables (clients, projects, users, members), `TenantScope`, repositories |
| `middleware` | request context, security headers, JWT auth, tenant headers, service auth, tool policy |
