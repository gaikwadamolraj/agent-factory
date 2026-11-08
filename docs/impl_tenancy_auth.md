# Tenancy, auth and zero trust

Token types, keys, rotation, client_credentials and the versioned authz cache: [security_auth.md](security_auth.md).

* **Model**: `clients` (tenants) -> `projects`; `users` belong to one client and are `project_members`.
* **User auth** (auth-service): `POST /api/v1/auth/login` sets two httpOnly, SameSite=Strict cookies: `ats_access`
  (ES256 JWT `aud=ats-api`, 5 min) and `ats_refresh` (`aud=ats-refresh`, 1 day, jti allow-listed in Redis). The UI
  refreshes silently (`POST /auth/refresh`) at 80% of the access lifetime and once on any 401; refresh tokens are
  single use (rotation; a replayed token is rejected). `POST /auth/logout` revokes and clears. JavaScript never
  sees a token. API clients may send `Authorization: Bearer`. Cookie-authenticated writes must carry
  `X-Requested-With` (CSRF guard). Failed logins lock a username for 15 minutes (`AUTH-RTL-0001` + `retryAfter`).
* **Verification**: every service verifies the token locally with the auth-service JWKS (`JwksVerifier`), and
  rejects tokens older than the user's authz version (`AUTH-EXP-0002`, the UI refreshes).
* **Tenant scope**: every `/api/v1/chats*` call carries `X-Client-Id` + `X-Project-Id`. `TenantMiddleware` checks the
  client equals the token's `cid` and that the user is active and a member of that active project, from the
  `agent_common.authz` snapshot of the user's current authz version (Redis, database only on a miss). Handlers receive a
  `TenantScope`; tenant tables are only queried through `TenantRepository`, which adds `client_id` + `project_id` to
  every statement. A chat of another project is indistinguishable from "not found".
* **User management** (auth-service `/api/v1/admin/users`): admins of a client only (role admin in token and
  snapshot); every change bumps the user's authz version, so it applies at once in every service.
* **Service-to-service**: the orchestrator gets a 5-minute service token per target from auth-service
  (client_credentials, `aud=agent-skill`, `sub=ats-orchestrator`) and reuses it until 80% of its lifetime.
  The agent's `ServiceAuthMiddleware` verifies it (JWKS, audience, `ALLOWED_CALLERS`) and `ToolPolicyMiddleware`
  allows only the tools configured for that caller (`TOOL_POLICY`). User tokens are rejected by the agent and vice
  versa.
* **Agent memory** is keyed per tenant: Strands session id `c{client}-p{project}-u{user}`.
* **Environments**: `APP_ENV=PROD` disables `/docs` + OpenAPI, the `X-Served-By` header and permissive CORS, and
  MCP debug mode.

## Row-level security — the backstop, and what makes it real

Application code scopes every query through `TenantScope` / `TenantRepository`. RLS is the backstop for the
query that forgets. Completed 8 November 2026 (migration `0005_rls_remaining_tables`).

**Before**: 14 tenant tables carried a policy and **10 did not** — and the ten were the ones holding the
customer data: `documents`, `document_blobs`, `document_chunks`, `chats`, `chat_memory`, `chat_turns`,
`screenings`, `knowledge_episodes`, `projects`, `users`. Every tenant table now has one.

`chat_messages` is the exception worth knowing about: it holds the conversation text and has **no
`client_id`** of its own, so it is scoped through the chat it belongs to with an `EXISTS` on the chats
primary key. Leaving it out would have made it the one unprotected table holding message content.

### A policy does not apply to the table's owner

This is the part that makes the difference between real isolation and decoration, and it was measured, not
assumed. With two tenants seeded and `app.client_id` set to the first:

| Connected as | Sees |
|---|---|
| `postgres` (owns the tables) | **2 chats, 2 messages** — both tenants |
| `app_login` (member of `ats_app`, owns nothing) | **1 chat, 1 message, 1 user** — its own only |

So the migration also creates the `ats_app` group role with `SELECT/INSERT/UPDATE/DELETE` and default
privileges for future tables, but **no LOGIN and no password** — a credential in a migration is a credential
in version control. The deployment creates the login role and grants it membership.

**The application's `DATABASE_URL` must use a non-owner role.** Connecting as the owner leaves every policy
in this migration inert.

Also verified on the same database:

- **Fail-closed**: with no `app.client_id` set at all, the app role sees **0 rows**, not all of them.
- **Writes are checked, not just reads**: inserting a row for another tenant raises *"new row violates
  row-level security policy"* — the `WITH CHECK` half.
- **The app role cannot change the schema**: `DROP TABLE` is refused with *"must be owner of table"*. Schema
  changes stay the migration job's alone.
- The migration downgrades and re-upgrades cleanly.
