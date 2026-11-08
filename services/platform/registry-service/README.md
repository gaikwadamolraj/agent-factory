# registry-service

The platform **admin & authoring plane** (FastAPI + MCP, package `registry_service`, port 8005, namespace
`ats-platform`). One place to configure the platform: skills and their workflows, who may do what (IAM-style policy),
delegated user/client/project administration, notifications, and a proxy over other MCP servers. The recruiter app
(`ats-orchestrator` + `ats-ui`) stays a pure runtime; the admin UI (`admin-ui`) is its own surface and talks only to
this service. See `docs/impl_registry_service.md` (authoritative design).

## One contract, two front doors

Every capability is defined once as an **MCP tool** (`registry_service/catalog.py`) and reached two ways, both enforcing
the **same auth + policy** through one shared pipeline (`registry_service/dispatch.py`):

1. **MCP front door** - the MCPServer mounted at `/mcp` behind the repo's secure wrapper (service token, aud
   `registry-service`, `ALLOWED_CALLERS` + per-tool `TOOL_POLICY`). For agents/services (none today; default deny).
2. **REST wrapper** - `POST /api/v1/factory/{tool}` turns each tool into an endpoint from its request model, with no
   per-tool handler code. This is the hardened main attack surface (auth, rate-limit hook, strict CORS, no `/docs` in
   PROD, request-size cap, audit, error envelope only).

Authorization is **default deny**: a user call is evaluated by the IAM engine (`agent_common.policy`) against the tool's
`action`+`resource`; a service call is checked against the machine `TOOL_POLICY`. A tool with no mapping is refused.

A tool-catalogue proxy (`POST /api/v1/mcp/{server}/{tool}`) browses/invokes tools on other registered MCP servers
through the same pipeline.

## Delegated admin & identity

registry-service owns clients, projects, groups, policies, skills and notifications. **Identity writes** (create user,
set role, deactivate, invite) are delegated to the **auth-service** over a service token (aud `auth-service`) - see
`registry_service/identity_client.py`. Roles are seeded policy bundles: `super_admin` (wildcard, cross-tenant) and
`client_admin` (scoped by the `same_client` condition). A super admin may act in another client via `X-Client-Id`
(audited); everyone else is pinned to their own client by the resolved tenant context + RLS.

## Run locally

```
cp .env.example .env          # fill DATABASE_URL / REDIS_URL / AUTH_* (never commit .env)
uv sync
PYTHONPATH=src uv run uvicorn registry_service.server:app --port 8005
```

Health: `GET /healthz`. Tools: `POST /api/v1/factory/{tool}` with `Authorization: Bearer <access token>` and
`X-Client-Id`. The database tables are owned by `services/platform/schema` (Alembic 0001); the bootstrap admin
policies are in `services/platform/schema/seed/002_platform_admin.sql` (dev seed, idempotent, not auto-run in PROD).

## Layout

| File | Role |
|------|------|
| `config.py` | settings (`FactorySettings`) |
| `catalog.py` | the one table: tool name -> request model, handler, action/resource, flags |
| `operations/` | framework-free business logic (skills, policies, groups, notifications, admin, catalogue) |
| `authorization.py` / `dispatch.py` | the shared auth+policy+dispatch pipeline (one authority) |
| `subjects.py` / `principals.py` | policy Subject + principal (user/service) |
| `identity_client.py` | registry-service -> auth-service identity contract |
| `catalogue.py` | registered-MCP-server proxy (refreshable cache) |
| `tools.py` | MCP front door (registers every catalog tool) |
| `api.py` / `server.py` | REST wrapper + app composition |
| `audit.py` | audit mixin stamping + `factory.audit` events |
