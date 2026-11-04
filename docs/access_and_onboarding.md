# Access, roles and onboarding

This guide explains who can see what in the ATS platform and how to onboard clients, projects and users. It also
covers the Grafana logins and which screening skills exist.

Everything here works today. Steps marked **SQL** have no screen or API yet.

## How access works

Access is decided in three layers:

| layer | stored in | decides |
|---|---|---|
| Client (organisation) | `clients` | which organisation a user belongs to; users never see another client's data |
| Role | `users.role` | what a user may do inside their client |
| Project access | `project_members` (one row per user per project) | which projects a user can work in |

The role does **not** decide project access. An admin sees only the projects they are a member of, like
everyone else. In the demo data the admin happens to be a member of both Acme projects.

The server checks project membership on every request (the `X-Client-Id` / `X-Project-Id` headers must match a
membership), so a membership change applies to the next request. The project dropdown in the UI refreshes when
the user reloads the page.

### Roles

| role | can |
|---|---|
| `admin` | everything a recruiter can, plus the **Manage users** screen (users of their own client only) |
| `recruiter` | chats, uploads, screenings and questions in the projects they are a member of |

Admin actions are re-checked against the database on every call, so a demoted or disabled admin loses the
Manage users screen at once. Other role and enable/disable changes reach a user's session within about
15 minutes (the access-token lifetime). A disabled user can't sign in or refresh their session.

### Demo data (DEV only)

| user | role | projects |
|---|---|---|
| `acme.admin` | admin | Engineering hiring, Pharma hiring |
| `acme.recruiter` | recruiter | Engineering hiring |
| `globex.recruiter` | recruiter | Sales hiring |

The demo password is in the main [README](../README.md). The demo seed never runs in PROD.

## Managing users (admin screen)

Sign in as an admin and click **Manage users** at the bottom of the sidebar. In the collapsed rail it is the
people icon.

| action | how |
|---|---|
| Add a user | fill in username, optional email, password (min 8) and role, then **Add user** |
| Change role | role dropdown in the user's row |
| Disable / enable | the **Active** / **Disabled** button in the row |
| Reset password | key icon in the row |

Rules:
- A new user gets access to **all active projects of the client** at the moment they are created. Projects
  added later have to be granted with SQL (see below).
- Admins can't change their own role or disable themselves.
- The last active admin of a client can't be demoted or disabled (`USR-INV-0001`).
- Usernames are unique across all clients (`USR-CNF-0001`).

API: `GET /api/v1/admin/users`, `POST /api/v1/admin/users`, `PATCH /api/v1/admin/users/{id}`. Normal users get
`403 AUTH-FBD-0001`.

## Granting or removing project access (SQL)

Connect to the database:

```bash
kubectl -n amol exec -it postgres-0 -- psql -U ats_admin -d ats_platform
```

Give a user access to a project (example: `acme.recruiter` to Pharma hiring):

```sql
INSERT INTO project_members (project_id, user_id)
SELECT p.id, u.id
FROM users u
JOIN projects p ON p.client_id = u.client_id AND p.slug = 'pharma'
WHERE u.username = 'acme.recruiter'
ON CONFLICT DO NOTHING;
```

Remove it:

```sql
DELETE FROM project_members
WHERE user_id = (SELECT id FROM users WHERE username = 'acme.recruiter')
  AND project_id = (SELECT p.id FROM projects p JOIN clients c ON c.id = p.client_id
                    WHERE c.slug = 'acme' AND p.slug = 'pharma');
```

See who has access to what:

```sql
SELECT c.slug AS client, u.username, u.role, p.name AS project
FROM users u
JOIN clients c ON c.id = u.client_id
LEFT JOIN project_members m ON m.user_id = u.id
LEFT JOIN projects p ON p.id = m.project_id
ORDER BY 1, 2, 4;
```

## Onboarding a new client (SQL)

1. Create the client and its projects:

   ```sql
   INSERT INTO clients (slug, name) VALUES ('newco', 'NewCo Hiring');

   INSERT INTO projects (client_id, slug, name, description)
   SELECT id, 'pharma', 'Pharma hiring', 'QA, validation and clinical roles' FROM clients WHERE slug = 'newco';
   ```

2. Generate a password hash for the client's first admin on your machine. Passwords are stored only as bcrypt
   hashes:

   ```bash
   cd services/orchestrator
   uv run python -c "from ats_common.security import hash_password; print(hash_password('<password>'))"
   ```

3. Create the first admin and give them all of the client's projects:

   ```sql
   INSERT INTO users (client_id, username, email, password_hash, role)
   SELECT id, 'newco.admin', 'admin@newco.example', '<hash from step 2>', 'admin'
   FROM clients WHERE slug = 'newco';

   INSERT INTO project_members (project_id, user_id)
   SELECT p.id, u.id
   FROM users u JOIN projects p ON p.client_id = u.client_id
   WHERE u.username = 'newco.admin'
   ON CONFLICT DO NOTHING;
   ```

4. From here the client's admin creates the other users in **Manage users**.

To add a project to an existing client, run the `projects` insert from step 1, then grant it to users with the
access SQL above. To retire a project or client without deleting data, set `is_active = false`: inactive
projects and clients are refused by the membership check.

## Grafana logins

Grafana (`/grafana`) requires a sign-in. Anonymous access is off.

| login (from `ats-platform-secrets`) | Grafana role | can |
|---|---|---|
| `grafana-admin-user` / `grafana-admin-password` | Admin | view and edit dashboards, manage Grafana users and data sources |
| `grafana-viewer-user` / `grafana-viewer-password` | Viewer | view dashboards, logs and traces only |

Both accounts are created from the secret every time the Grafana pod starts. To change a password, update the
secret and run `kubectl -n ats-platform rollout restart deployment/ats-lgtm`. The **ATS Platform Overview**
dashboard (folder **ATS**) is loaded automatically from
`deploy/k8s/base/observability/dashboards/ats-platform.json`. Edits made in the Grafana UI are lost on restart,
so export them into that file to keep them.

These Grafana logins are separate from the ATS app users.

## Screening skills (per industry)

There is one skill: `ats_screening` version `v1` (`services/agent/src/ats_agent/skills/ats_screening`). It isn't
tied to any industry: it reads the job description to decide what matters (for example GMP, validation, clinical
trials or regulatory work for pharma) and scores the resume against that. Pharma roles therefore work today,
judged on what the pharma JD asks for.

Not available yet:
- **Choosing a skill per project.** Every project uses the agent's default skill (`DEFAULT_SKILL_NAME` /
  `DEFAULT_SKILL_VERSION` in the agent config). The agent's `screen_candidate` tool accepts a `skillVersion`, but
  the orchestrator doesn't send one.
- **An industry-specific skill**, for example a pharma variant that treats GxP or 21 CFR Part 11 as must-haves.

## Not built yet

| need | today | possible next step |
|---|---|---|
| Project access per user | SQL | project checkboxes per user in **Manage users** |
| New clients and their first admin | SQL | platform super-admin role with a client/project screen |
| Skill per project or industry | one default skill | per-project skill setting passed by the orchestrator to the agent, plus industry skill variants |
