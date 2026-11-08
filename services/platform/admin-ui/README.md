# admin-ui

The platform **admin & authoring console** (Next.js 14, app router, package name `admin-ui`, port 3000, namespace
`ats-platform`). It is separate from the recruiter app (`ats-ui`), which stays a focused runtime. admin-ui talks to
**registry-service** (skills/versions/approvals, IAM policy & groups, delegated user/client/project admin, notifications,
MCP tool-catalogue) and to **auth-service** (login/refresh/logout/me and invite accept) — nothing else. It is reachable
on its own route `/admin` on the shared host. See `docs/impl_agent_factory.md` §9 (authoritative).

## Security model (the UI is not the boundary)

Tokens never reach the browser. Sign-in sets httpOnly cookies on the auth-service; every API call goes through the
**server-side proxy** at `/admin/api/backend/*` (`src/app/api/backend/[...path]/route.ts`):

- `v1/auth/*` → auth-service, cookies forwarded both ways (it rotates the session cookies).
- everything else (`v1/factory/*`, `v1/mcp/*`, `v1/notifications`) → registry-service, which authenticates from an
  `Authorization: Bearer` header. The proxy lifts the access token out of the `ats_access` cookie into that header, so
  the token still never reaches browser JavaScript.

The console gates admin screens by role for UX, and shows "not authorized" to non-admins, but the **real** enforcement
is server-side in registry-service (IAM policy, default deny). A `401` with a retryable envelope triggers one silent
refresh and a retry, mirroring ats-ui.

## Screens → registry-service tools

| Screen | Tools called |
|--------|--------------|
| Skills & versions | `list_skills`, `get_skill`, `create_skill`, `create_draft_version`, `update_draft_version`, `get_skill_version`, `submit_version`, `approve_version`, `reject_version`, `activate_version`, `archive_version`, `set_version_testers` (+ client-side diff of two versions' workflow) |
| Approvals queue | `list_skills` + `get_skill` (collect `in_review`), `approve_version`, `reject_version` |
| Notifications | `list_notifications`, `mark_notification_read` |
| Policies & groups | `list_policies`, `create_policy`, `update_policy`, `delete_policy`, `simulate_policy`, `list_groups`, `create_group`, `delete_group`, `add_group_member`, `remove_group_member` |
| Users / roles | `invite_user`, `list_invites`, `revoke_invite`, `create_user`, `set_user_role`, `deactivate_user` |
| Clients / projects | `list_clients`, `create_client`, `assume_client` (super admin), `list_projects`, `create_project`, `add_project_member` |
| Tool catalogue | `list_catalogue`, then `POST /api/v1/mcp/{server}/{tool}` |

Draft **test-run** is not a registry-service tool (running a skill is agent-skill's job, driven from the recruiter flow
with the version pinned); the UI instead manages the draft **tester allow-list** (`set_version_testers`). Listing
**active** users is not exposed by registry-service either, so role/deactivate take a user id and the screen lists pending
invites.

## Run locally

```
cp .env.example .env.local     # set INTERNAL_REGISTRY_URL / INTERNAL_AUTH_URL
npm install
npm run dev                    # http://localhost:3001/admin
```

Production build: `npm run build` (Next standalone output; the Dockerfile mirrors ats-ui).

## Config (env, server-side only)

| Var | Meaning |
|-----|---------|
| `NEXT_BASE_PATH` | base path the app is served under (default `/admin`; must match the chart route) |
| `INTERNAL_REGISTRY_URL` | registry-service base URL (`http://registry-service` in-cluster) |
| `INTERNAL_AUTH_URL` | auth-service base URL (`http://auth-service` in-cluster) |
| `MAX_REQUEST_MB` | request-size cap the proxy enforces before reading the body |
