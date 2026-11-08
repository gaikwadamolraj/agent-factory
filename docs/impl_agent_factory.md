# registry-service — admin & authoring plane (DESIGN approved 2026-11-06; BUILT; renamed 2026-11-07)

> **Renamed 2026-11-07.** This service was called `agent-factory` while that name was free. `agent-factory` is
> now the platform itself (repo, Helm chart, namespace, Secret), so the service became `registry-service`.
> Later mentions of "agent-factory" below mean this service, unless they clearly mean the namespace, the chart
> or the `/agent-factory` admin-console URL. Target role and roadmap: docs/impl_domain_platform.md.

`registry-service` (services/platform/registry-service, FastAPI, package `registry_service`, port 8005, namespace
`agent-factory`) is the one place admins configure the platform: skills and their workflows, who may do what
(IAM-style policy), notifications, and later the agent registry. The recruiter app (`ats-orchestrator` + `ats-ui`)
stays a pure runtime. The admin UI is its own surface (see §9), not mixed into the recruiter UI.

This is built to be **product-neutral** so the same service governs ATS today and other domains later; it reuses the
shared building blocks in §7 that the broader platform vision also needs.

## 1. Skills, versions, lifecycle

A **skill** is a named, domain-scoped capability (e.g. `ats/screening`). It has many **versions**; a version holds
the workflow definition and its files (per docs/impl_agent_platform.md: `skill.yaml`, prompts, schemas, rules,
`code/*.py`). Released versions are immutable (CLAUDE.md §10): you branch a new draft, never edit a published one.

Version states and transitions:

```
draft ──submit──► in_review ──approve──► published ──archive──► archived
  ▲                   │
  └──────reject───────┘
```

- **default resolution:** a run with no version pin uses the **latest `published`** version of the skill.
- **draft visibility/run:** a `draft` version is visible and runnable **only** by its author and an explicit
  tester allow-list, and only when the caller pins that exact version. It never becomes the default.
- **activate from UI:** an admin may set which published version is the active default, archive a version, or roll
  back to an earlier published one. Activation writes `skills.active_version_id` and **bumps the skill cache
  version** (§8) so every service picks it up at once.

## 2. Approval flow + notifications

- Author submits a draft -> `in_review`. Approvers (resolved from policy, §4: action `skill.approve` on that
  skill's domain/project) receive a **notification**.
- Approver approves (-> `published`, becomes activatable) or rejects (-> `draft`, with a reason). The author is
  notified of the outcome. All transitions are recorded in `skill_approvals` (who, when, from/to state, comment).
- Notifications are generic so every feature reuses them: `notifications(id, external_id, tenant/client/project,
  recipient_user_id, type, title, body, link, actor_user_id, is_read, created_at, …)`. The UI "notifications" tab
  lists unread first; marking read is an API call. Delivery = DB write + push on the existing SSE channel (no email
  this phase).

## 3. FastMCP tools + a hardened API wrapper (one contract, two front doors)

agent-factory's operations are defined ONCE as **FastMCP tools** (the tool layer): every admin capability (skills,
versions, approvals, policy/groups, users/clients/projects, notifications, invites, tool catalogue) is a FastMCP
tool with a typed input/output schema. That one definition is reached two ways, and **both enforce the same auth
and policy** (shared code, no second source of truth):

1. **MCP front door** (for agents, now and future): the FastMCP server mounted with the standard secure wrapper -
   service token (aud `agent-factory`) + `ALLOWED_CALLERS` + tool policy. A future agent can drive agent-factory
   over MCP directly.
2. **API wrapper** (for the admin UI and external callers): a generated REST surface that turns each FastMCP tool
   into `POST /api/v1/factory/{tool}` from the tool's own JSON schema, so adding a tool adds an endpoint with no new
   handler code.

Use the MCP stack already in the repo (check the `agent_common` MCP helpers + the agents' servers; confirm the exact
FastMCP API with Context7 before coding).

**The API wrapper is the main attack surface, so it is hardened - not a thin pass-through:**
- **AuthN:** user calls carry the user JWT (aud `ats-api`); service/agent calls carry a service token (aud
  `agent-factory`). Both verified locally via `JwksVerifier` (no call to auth-service) + the `authz_ver` staleness
  check. No token, no entry.
- **AuthZ (every tool, default deny):** each tool is mapped to a required `action` + `resource` and run through the
  IAM evaluator (`agent_common.policy`, §4). A tool with no policy mapping is refused - nothing is exposed implicitly.
  Delegated-admin scoping (§6b) applies here: client-admin calls are forced to their own client.
- **Input validation at the edge:** request bodies validated against the tool's schema, `extra="forbid"`, with
  length/size/enum bounds; snake_case in, camelCase on the wire (`ApiModel`/`ApiResponse`).
- **Tenant scoping:** `X-Client-Id`/`X-Project-Id` checked against claims; the RLS GUC is set from verified claims
  so the DB enforces isolation even if a check is missed.
- **Limits & transport:** request-size cap, per-principal rate limit (Traefik + app), strict CORS in PROD, no
  `/docs` in PROD. The internal MCP transport is never exposed unauthenticated; the wrapper re-checks auth+policy
  and never trusts the MCP layer or `X-Auth-*` headers for authorization.
- **Audit & errors:** every mutating tool writes an audit record (actor, before/after); responses use the error
  envelope only - never stack traces or internal names (CLAUDE.md §5).

Separately, a **tool catalogue** proxy lets the admin UI browse and (within policy) invoke tools on OTHER registered
MCP servers (agent-skill, agent-compute, ...) via `POST /api/v1/mcp/{server}/{tool}`, validated from their schemas
and cached/refreshable - the same auth+policy pipeline as above.

## 4. Tool/skill policy — IAM-style, no repeated rules

One evaluator, used everywhere (orchestrator, agent-factory, the MCP tool-policy middleware), lives in
`agent_common.policy`. Policies are **data**, editable from the UI; the engine is code.

Tables:
- `groups(id, external_id, tenant/client, name, …)`, `group_members(group_id, user_id)`.
- `access_policies(id, external_id, tenant/client, principal_type, principal_id, document JSONB, enabled, …)`
  where `principal_type ∈ {user, group, project, client}`.

Policy document (IAM-shaped):
```json
{ "statements": [
  { "effect": "allow", "actions": ["skill.run", "skill.read"], "resources": ["skill:ats/*"] },
  { "effect": "allow", "actions": ["skill.approve"], "resources": ["skill:ats/screening"] },
  { "effect": "deny",  "actions": ["skill.run"], "resources": ["skill:*:draft"],
    "conditions": { "unless_author": true } }
] }
```
Evaluation for (subject, action, resource):
1. collect every enabled policy attached to the subject and to each group/project/client it belongs to;
2. **explicit `deny` wins**; else an `allow` must match; else **default deny**;
3. conditions are a small, fixed, documented set (own-resource, project membership, draft-author) — not arbitrary
   code. An OPA backend can be added later behind the same interface (user decision: in-code now).

Actions/resources are namespaced strings (`skill.run`, `tool:agent-compute.run`, `admin.users.manage`,
`policy.manage`, `skill.approve`). The existing service-to-service `ALLOWED_CALLERS`/`TOOL_POLICY` stay as the
machine layer; this table is the human layer on top.

## 5. Database (new tables only; conventions per user decision)

New agent-factory tables follow the platform-wide conventions: `id BIGSERIAL` internal PK (joins only, never
exposed) + `external_id` (UUID, or ULID for time-ordered rows) exposed to the API; an **audit mixin**
(`created_at/by, updated_at/by, is_deleted, deleted_at/by, deleted_reason` — soft delete); **RLS** on `tenant_id`
from a session variable set by verified claims. Tables: `skills`, `skill_versions`, `skill_version_files`,
`skill_approvals`, `notifications`, `groups`, `group_members`, `access_policies`. (Existing ATS tables keep their
current shape; retrofitting them is a separate, later migration.) All added to the single fresh
`services/platform/schema/migrations/versions/0001_initial_schema.py` while it is still unreleased.

Deferred execution tables (Temporal phase, designed now so they slot in): `workflow_runs`, `artifacts`,
`artifact_subjects` (reuse outputs to answer questions without re-running) — see docs/impl_agent_platform.md.

## 6. Admin capabilities exposed to the UI

Everything an admin needs, through agent-factory's API: manage users/roles (delegating writes to auth-service),
groups, projects, policies; author/submit/approve/activate/archive skills and versions; browse and run the MCP
tool catalogue (within policy); read notifications; view the model/gateway config (read-only here; model
management is a separate item). Each endpoint is guarded by the policy engine and a service/user token check.

## 6b. User, client & group management (delegated admin)

Roles are **seeded policy bundles** (§4), not a separate mechanism — "make X an admin" just attaches a scoped
policy. The admin-ui calls agent-factory for all of this; agent-factory calls auth-service for identity writes
(users, roles, passwords, invites) and owns clients, projects, groups and policies itself.

Roles:
- **super admin** (the platform owner): every action across every client; the only role that can create clients,
  grant/revoke super admin, and act cross-tenant.
- **client admin**: scoped to one `client_id`. Within that client may: invite/create and deactivate users; create,
  edit and archive **projects** and manage their membership; create and manage **groups** and their members; and
  grant/revoke **client admin** to that client's users. Cannot see or affect any other client.

Escalation guardrails (hard):
- nobody grants a role broader than their own;
- every client-admin write is forced to their own `client_id` by RLS **and** a same-client policy condition,
  whatever the request body says;
- only super admin creates clients or manages super admins;
- all admin actions are audited (actor, time, before/after) via the audit mixin.

Cross-tenant for super admin: RLS stays on for everyone. The UI lets a super admin **select a client to act in**;
agent-factory sets that tenant context for the operation (audited). A platform-only "list/manage all clients" path
is guarded by the super-admin policy and runs a dedicated cross-tenant query — the only place that bypasses the
per-tenant predicate.

Invite flow (user decision: invite by email link, built to work before a mail server exists):
- client admin (or super admin) invites an email into their client with a chosen role/groups/projects;
- auth-service creates a pending user + a **signed, expiring invite token** and always returns a copyable invite
  link plus an in-app notification to the inviter;
- delivery is a **pluggable notifier** (`agent_common.notifications` backend): logs/returns the link today, sends
  real email once an SMTP/provider backend is configured — no caller change (same pattern as `agent_common.secrets`);
- the invitee opens the link, sets their own password (gets the versioned pepper, §10), and the invite is consumed
  (single-use, expiring). Tables: `user_invites(id, external_id, tenant/client, email, role, groups, projects,
  token_hash, status, expires_at, invited_by, …)`.

## 7. Shared packages in `agent_common` (reusable platform layer)

- `agent_common/policy/` — the evaluator in §4 (interface + in-code backend; OPA backend later).
- `agent_common/secrets/` — one `get_secret(name)` interface, backend chosen by config: k8s Secret today;
  OpenBao / GCP / AWS / Azure as future drivers, no caller change (matches the broader platform vision).
- `agent_common/notifications/` — the notifications model + a small `notify(...)` helper.
- reuse existing `agent_common.errors`, `agent_common.authz` (version-bump cache), `agent_common.db`.

## 8. Caching (per the agreed plan)

Every cache key carries a **version** bumped on change, so all replicas read the latest with no stale windows:
- skill resolution (active version per skill) — `skillver`;
- compiled policy per subject — `policyver` (bump on any policy/group/membership change);
- the MCP tool catalogue — refreshable + versioned.
Reads hit Redis; a short in-process TTL avoids a round trip per request. Same mechanism as `authz_ver` and
`ConfigVersion`.

## 9. UI

Admin UI is a **dedicated service `admin-ui`** (Next.js, `services/platform/admin-ui`, namespace `agent-factory`),
separate from the recruiter `ats-ui` (recruiter stays focused). It talks only to agent-factory (+ auth-service for
login) and is reachable on its own route (e.g. `/admin`), guarded so only users with admin policy can load it.
Screens: skills & versions (editor, diff, submit/approve/activate, draft test-run), approvals queue, notifications
tab, policies & groups (IAM editor with a "simulate: can user X do action Y on resource Z" check), users/roles,
tool catalogue.

## 10. Password hardening (auth-service)

bcrypt (per-password salt) gains a **versioned server-side pepper**: `bcrypt(HMAC_SHA256(pepper_vN, password))`.
Peppers are versioned secrets; the stored hash records its pepper version; on a correct login under an older
pepper the hash is transparently re-written with the newest. Rotation (add `pepper_vN+1`, mark it current) never
locks anyone out. Pepper list comes from the secret store (§7).

## 11. Build order (deploy stays on hold)

1. restructure services into groups (separate task, in progress).
2. `agent_common`: policy evaluator, secrets interface, notifications.
3. DB: new tables in the fresh 0001 migration.
4. `agent-factory`: skills/versions/approvals, notifications, policy admin, MCP-to-API wrapper, admin endpoints.
5. auth-service: versioned pepper + rehash-on-login.
6. admin UI.
7. tests throughout; deploy everything together only when the user says go.

## Explicitly deferred

Temporal execution, OPA-as-a-service, Langfuse, PgBouncer, async-asyncpg conversion, A2A / bring-your-own-agent
runtime, `agent-document`, OpenShell-style sandbox. Designed around, not built now.
