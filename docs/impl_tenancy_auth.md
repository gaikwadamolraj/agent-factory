# Tenancy, auth and zero trust

* **Model**: `clients` (tenants) -> `projects`; `users` belong to one client and are `project_members`.
* **User auth**: `POST /api/v1/auth/login` sets two httpOnly, SameSite=Strict cookies: `ats_access` (JWT
  `aud=ats-api`, 15 min) and `ats_refresh` (JWT `aud=ats-refresh`, 7 days, jti allow-listed in Redis). The UI
  refreshes silently (`POST /auth/refresh`) at 80% of the access lifetime and once on any 401; refresh tokens are
  single use (rotation; a replayed token is rejected). `POST /auth/logout` revokes and clears. JavaScript never
  sees a token. API clients may send `Authorization: Bearer`. Cookie-authenticated writes must carry
  `X-Requested-With` (CSRF guard). Failed logins are throttled per username (`AUTH-RTL-0001` + `retryAfter`).
* **Tenant scope**: every `/api/v1/chats*` call carries `X-Client-Id` + `X-Project-Id`. `TenantMiddleware` checks
  the client equals the token's `cid` and that the user is a member of that active project (cached 5 min in
  Redis). Handlers receive a `TenantScope`; tenant tables are only queried through `TenantRepository`, which adds
  `client_id` + `project_id` to every statement. A chat of another project is indistinguishable from "not found".
* **Service-to-service**: the orchestrator mints a 5-minute service token (`aud=ats-agent`, `sub=ats-orchestrator`)
  per MCP session. The agent's `ServiceAuthMiddleware` verifies it and `ToolPolicyMiddleware` allows only the
  tools configured for that caller (`TOOL_POLICY`). User tokens are rejected by the agent and vice versa.
* **Agent memory** is keyed per tenant: Strands session id `c{client}-p{project}-u{user}`.
* **Environments**: `APP_ENV=PROD` disables `/docs` + OpenAPI, the `X-Served-By` header and permissive CORS, and
  MCP debug mode.
