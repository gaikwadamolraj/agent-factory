# Resume here — Friday 13 November 2026

Working notes for picking this back up. Branch `feat/version2`. **Nothing is committed yet** — the whole
multi-domain restructure is an uncommitted working tree. That is the first decision to make on Friday.

---

## 1. How to run it

### Local stack (docker compose)

```bash
./scripts/local_env.sh            # once: creates .env and generates secrets (never printed)
docker compose up -d --build
```

| What | URL | Notes |
|---|---|---|
| ATS UI | http://localhost:3000/ats | `acme.recruiter` / `acme.admin` / `globex.recruiter` |
| Admin console | http://localhost:3002/agent-factory | skills, versions, domain packs, tool catalogue (admin role) |
| Grafana | http://localhost:3001/grafana | dashboard "ATS Platform Overview" |
| registry-service | http://localhost:8005 | API directly |
| auth-service | http://localhost:8004 | API directly |

LM Studio must be running on the host for anything that calls a model:
`lms server start --bind 0.0.0.0 --port 1235`.

### ⚠ Compose needs its Postgres volume recreated once

The `ats-platform_pgdata` volume is from the **old** migration chain. The schema job stops with:

```
alembic.util.exc.CommandError: Can't locate revision identified by '0002_knowledge_episodes'
```

That revision no longer exists — the chain is now `0001_initial_schema → 0002_subjects →
0003_workflow_runs → 0004_match_results → 0005_rls_remaining_tables`. The old tables are already there, so
the new chain cannot apply on top. Recreate the volume (this **discards local dev data**; the demo seed
rebuilds the tenants and users):

```bash
docker compose stop && docker compose rm -f postgres
docker volume rm ats-platform_pgdata
docker compose up -d
```

A `pg_dumpall` backup of the old volume is at
`C:\Users\gaikw\AppData\Local\Temp\claude\f3664c52d4cb8723\9f42f7a3-bba1-4e98-b603-f01d1aa59278\scratchpad\compose-pg-backup.sql`
(~648 KB) — temp directory, so copy it somewhere permanent if you want to keep it.

**Two password traps, both already hit:**

1. Postgres only applies `POSTGRES_PASSWORD` when it *first initialises its volume*. After
   `local_env.sh` generates a new one, every service fails with `password authentication failed`.
2. `docker exec`-ing into a long-running postgres container reads that container's **old** environment, so
   an `ALTER USER` there sets the password back to the stale value. Recreate the container first.

```bash
docker compose up -d --force-recreate postgres
printf 'psql -v ON_ERROR_STOP=1 -U ats -d postgres -c "ALTER USER ats WITH PASSWORD '"'"'$POSTGRES_PASSWORD'"'"'"\n' > /tmp/setpw.sh
docker cp /tmp/setpw.sh ats-platform-postgres-1:/tmp/setpw.sh
MSYS_NO_PATHCONV=1 docker exec ats-platform-postgres-1 sh /tmp/setpw.sh
docker compose up -d
```

Note that `psql -U ats -h 127.0.0.1` *inside* the container succeeds via `trust` in `pg_hba.conf`, so it
proves nothing about the password — always test from another container.

### Kubernetes (k3d, cluster `my-cluster`)

On a new machine, in order:

```bash
./deploy.sh doctor                    # says exactly what is missing, and the command to fix each thing
./deploy.sh init --generate-secrets   # namespaces, generated secrets, and the local registry (dev)
./deploy.sh                           # build every image, then deploy
./deploy.sh agent-skill ats-ui        # build just these, deploy; everything else keeps its tag
./deploy.sh apply                     # no builds at all: chart/config changes only
```

**Faster builds — the local registry.** `k3d image import` copies a whole image into every node on every
build. With a registry it pushes only changed layers:

```bash
./scripts/local_registry.sh           # one-time: create it, join the k3d network, trust it on each node
./deploy.sh agent-skill               # detected automatically - pushes instead of importing
```

It is idempotent — re-run it after recreating the cluster or the registry. It writes
`/etc/rancher/k3s/registries.yaml` into each node and restarts them, because containerd only reads that file
at start. **This has not been exercised end to end yet** — see §3.

---

## 2. Routes to test

**Status 9 Nov: the cluster side is done.** `./deploy.sh` finishes green (revision 11, "Upgrade complete"),
12 pods running, and every route answers in-cluster:

| Route | http | https |
|---|---|---|
| `/ats` | 200 | 200 |
| `/agent-factory` | 200 | 200 |
| `/grafana` | 302 (login) | — |

The only thing still blocking external access is the **Cloudflare 403** below. The history that follows is
kept because the same traps will recur on the next cluster.

### ✔ Resolved: the Traefik cross-namespace flag

**Until this is run, the new UI routes do not exist at all.** Traefik refuses an IngressRoute that points at
a Service in another namespace, and the chart's gateway lives in `agent-infra` while the UIs run in
`agent-factory`:

```
ERR  "service agent-factory/ats-ui not in the parent resource namespace agent-infra"
     ingress=ats-ui-route providerName=kubernetescrd
```

So `/agent-factory` returns 404, and `/ats` silently falls through to the **old `ats-platform` deployment** —
proved by ETag: via Traefik `a52wwnhyd1366` = old `ats-frontend`, while the new `ats-ui` serves
`p37oopw9pm366`. It looks like the new UI is up when it is not.

The fix is the documented one-time prerequisite in `deploy/review/amol-host/README.md`:

```bash
kubectl apply -f deploy/review/amol-host/traefik-gateway-config.yaml
kubectl -n kube-system rollout status deploy/traefik
```

(I could not run this myself — it relaxes a cluster-wide Traefik default, which this environment blocks.)

Afterwards, re-check with the in-cluster probe below; `/ats` should return the new ETag and
`/agent-factory` should stop 404ing. **Also decide what to do with the old `ats-platform` namespace** — it
still serves `/` and `/grafana` on the same host and will keep shadowing anything the new chart does not
match more specifically.

### Cloudflare

Host `amol.rackspire.ai` sits behind **Cloudflare**, which currently returns **403** to plain requests
(`Server: cloudflare`, Cloudflare block page — not your origin). The `x-api-key` header did not satisfy it.
In-cluster the same route returns 200, so this is a WAF rule to fix on the Cloudflare side.

**Verified working in-cluster** (this is the real proof the routing is fine):

```bash
kubectl run routetest --rm -i --restart=Never --image=curlimages/curl:8.11.1 -n agent-factory --command -- \
  curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: amol.rackspire.ai' \
  http://traefik.kube-system.svc.cluster.local/ats          # -> 200
```

| Path | Entry point | Protection | Serves |
|---|---|---|---|
| `/ats` | web **and** websecure | rate limits (stricter on auth + chat turns) | ats-ui |
| `/agent-factory` | **websecure only** | IP allow-list + basic auth + admin rate limit | admin-ui |
| `/grafana` | web | rate limit + security headers | Grafana (lgtm) |
| `/temporal` | websecure | admin protection | Temporal UI — **only when `infra.temporal.enabled`** |

Two things that look like bugs but are not:

- **`/agent-factory` 404s over plain http.** It is deliberately `websecure` only — the admin console shows
  every tenant's data. Use https.
- **The IngressRoutes live in `agent-infra`, not `agent-factory`.** `kubectl get ingressroute -n agent-factory`
  shows nothing and looks like they are missing. They are not:

```bash
kubectl get ingressroute,middleware -n agent-infra
```

### Port-forward (bypasses Traefik and Cloudflare entirely)

The Services listen on **port 80** and forward to the container's named `http` port (3000). Forwarding to
3000 on the *service* is what produces "connection refused" — there is no port 3000 on the Service.

```bash
kubectl port-forward -n agent-factory svc/ats-ui    8080:80   # then http://localhost:8080/ats
kubectl port-forward -n agent-factory svc/admin-ui  8081:80   # then http://localhost:8081/agent-factory
```

Remember the base path: `http://localhost:8080/` alone returns 404, because the UI is built with
`basePath=/ats`.

---

## 3. Open items, roughly in priority order

### Deployment

- [x] ~~Apply the Traefik cross-namespace flag~~ — done 9 Nov; routes register and answer.
- [x] ~~Retire the old `ats-platform` namespace~~ — deleted 9 Nov. Was hijacking the new UI's login and
      chat-message calls via `ats-platform-limited`. Formerly: — it still owns `/` and `/grafana` on
      `amol.rackspire.ai` and shadows the new stack.
- [x] ~~KEDA scales nothing~~ — fixed 9 Nov (secret now names `ats_platform`); the worker ScaledObject is
      Ready and two orphaned ScaledObjects were deleted. Was: `agent-knowledge-graph-worker`'s ScaledObject reports
      `relation "knowledge_episodes" does not exist`: the `database-url` secret names database
      `agent_factory`, but the schema lives in `ats_platform` (every service overrides it with
      `DATABASE_NAME`, and KEDA cannot). Fix the secret so its URL names `ats_platform`:
      `kubectl -n agent-factory edit secret agent-factory-secrets` (key `database-url`), then
      `kubectl -n keda rollout restart deploy/keda-operator`.
- [x] ~~Verify the local registry path end to end~~ — done 9 Nov. `./deploy.sh` **auto-detects** the local
      registry (container up, on the k3d network, and trusted in each node's `registries.yaml`) and pushes
      to it; it falls back to `k3d image import` when any of those fails, and `REGISTRY=none` forces the
      import. All 12 images are in the registry and every Deployment pulls `agent-factory-registry:5000/...`.
      **The trap it hit:** `global.image.registry` prefixes *every* component, so the first registry deploy
      broke on images that had only ever been imported. `mirror_unbuilt_images` now tags and pushes the
      already-built local images instead of forcing a full rebuild.
- [ ] **Cloudflare 403** on `amol.rackspire.ai`. Cluster-side routing is proven good; the rule needs fixing
      or the correct header documented.
- [ ] Decide whether `/agent-factory` should also answer on plain http in dev (currently https only).
- [ ] `values-prod.yaml` has never been exercised.

### The build plan (docs/impl_multi_domain.md §9)

1–6 are done. What is left:

- [ ] **7. Ingestion connectors** — beyond upload: S3 / SharePoint / database, cursors, schedules.
- [ ] **8. NL2SQL, banking, media, crawler.** The NL2SQL prototype is deliberately **not** wired into the
      chart, compose or deploy.sh: its gate is bypassable with
      `WITH x AS (DELETE ... RETURNING *) SELECT * FROM x`. Replace prefix matching with sqlglot AST
      validation before it goes anywhere near a real database.

### Finishing what is half-built

- [ ] **Insurance grader adapter** — the sibling of `ats-orchestrator/workflows/bulk_adapters.py`, and the
      one file a new domain genuinely has to write. The insurance pack validates and the shared engine runs
      it (docs/impl_insurance_pack.md), but no claim has been assessed end to end.
- [ ] **Publish the insurance pack through registry-service** — runtime pack resolution for a second domain
      is untested.
- [x] ~~A producer for J3~~ — done 9 Nov: `POST /api/v1/runs/bulk-match` starts one (202, reattaches on
      the same JD) and `GET /api/v1/runs/{id}/results` returns the ranked shortlist (failures excluded
      unless `includeFailed=true`). Still not run above 25 subjects, and no UI calls it.
- [ ] **ATS candidates are not migrated onto `subjects`** — the old `screenings`/chat model still holds them.
- [ ] Rest of Stage E: `agent-skill/screening/steps.py` still orchestrates in ATS terms
      (`analyze_job_description`, `match_requirements`) instead of pack-driven extract/grade/render; there is
      no `render_report(results, template)` and no report template in the ATS pack.
- [ ] `agent_common.rules` and `.match`, `agent_document.extractors.routing`, the J3 workflow and the
      `/api/v1/runs` endpoints have **no unit tests** — they were smoke-verified under the
      "skip tests this stage" instruction. That exemption should end.

### Smaller

- [ ] Only PDFs are routed by the extraction ladder; Docling reads PPTX/HTML/EPUB/images/email but
      `detect_kind` admits only PDF/DOCX/XLSX.
- [ ] No UI for durable runs beyond the per-resume status line; `api.listRuns` is unused.
- [ ] PgBouncer cleartext-password finding: mitigated with auth_query mode (docs/impl_pgbouncer.md).

---

## 4. What broke on 8–9 November, and why

Worth keeping, because two of these will recur.

1. **Every `helm upgrade` failed, so the routing never updated.** Two KEDA ScaledObjects
   (`ats-orchestrator-onboarding`, `ats-orchestrator-bulk-match`) pointed at Deployments that are disabled by
   default. A ScaledObject whose target does not exist never becomes ready, and `helm --wait` fails the whole
   release over it. Fixed: the ScaledObject template now follows the backend's own `enabled`, so there is one
   switch per worker instead of two that have to agree.
2. **KEDA could not reach Postgres** (`connection refused`). The `postgres-clients` NetworkPolicy admitted
   only `agent-factory` and `agent-infra`; the KEDA operator runs in the `keda` namespace. k3s *rejects*
   blocked traffic, which is why it looked like a dead database rather than a firewall. Fixed in
   `values-dev.yaml`.
3. **`helm upgrade` complained about its argument count.** `ownership_args` wrote a progress line to stdout
   and the caller captured stdout into the argument list, so the log line became a positional argument to
   helm. Fixed: `log`/`die` write to stderr, and the helm arguments are built in an array instead of being
   printed and split back apart.
4. **Every screening failed with "the screening agent is still unavailable".** Not infrastructure — a real
   bug: `AgentService._running` was initialised on a line *after* the `return` in
   `_build_registry_source`, so it never ran and `_register` raised `AttributeError` on every
   `screen_candidate`. Moved into `__init__`. Worth remembering that the symptom was a clean
   user-facing "service unavailable" message, which read like a network problem.
5. **A hand-edited Service blocked every deploy.** `kubectl edit` had changed `ats-ui`'s `targetPort` from
   the named port to `3000` (identical in effect), and server-side apply then refused to give the field back.
   `./deploy.sh` now passes `--force-conflicts` on every deploy: the chart owns its objects, and a manual
   edit is drift that a deploy should revert. Put deliberate changes in `values.yaml`.

---

## 5. Context worth not re-deriving

- **PyMuPDF is gone** (AGPL-3.0 / Artifex commercial) — replaced by pypdfium2 + pdfplumber + optional
  Docling. Do not reintroduce it. docs/impl_docling.md.
- **The app's `DATABASE_URL` must use a non-owner Postgres role**, or every row-level security policy is
  inert — Postgres does not apply policies to a table's owner. Migration `0005` creates the `ats_app` group
  role; the deployment grants a login role membership of it. docs/impl_tenancy_auth.md.
- **Industry code stays in its industry's service**; only what is common to every industry goes in
  `agent_common`. That is why `agent_common.rules` / `.match` carry no recruitment or insurance vocabulary.
- Traps that cost time are listed in the project memory file (`ats-pending-work.md`), including:
  `uv sync --frozen` does not pick up path-dependency changes (use `--reinstall-package agent-common`), and
  `perl -0pi -e 's|...|...|'` with unescaped `|` in the pattern silently clobbers files.
