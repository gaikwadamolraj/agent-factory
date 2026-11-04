# Kubernetes deployment

Kustomize, namespace **`ats-platform`**, files in `.yml`. Config (ConfigMap) lives in its own file; each service's
Deployment and Service are in one file.

```
deploy/k8s/
├── base/
│   ├── kustomization.yml        # namespace + observability + agent + orchestrator + frontend
│   ├── namespace.yml
│   ├── agent/                   # config.yml (ConfigMap) + deployment.yml (Deployment + Service)
│   ├── orchestrator/            # config.yml + deployment.yml
│   ├── frontend/                # config.yml + deployment.yml
│   ├── observability/           # Grafana LGTM: config.yml (dashboard provider) + lgtm.yml + dashboards/*.json
│   └── schema/                  # config.yml + job.yml (migration Job, applied separately by deploy.sh)
└── overlays/
    ├── dev/                     # APP_ENV=DEV, ingress (UI + Grafana); schema/ seeds demo tenants
    └── prod/                    # template: APP_ENV=PROD, registry images, strict CORS, no demo data
```

## Secret (created once, never committed)

`ats-platform-secrets` in `ats-platform` with keys:

| key | used by | value |
|---|---|---|
| `database-url` | schema, agent, orchestrator | `postgresql://USER:PASS@postgres-service.amol.svc.cluster.local:5432/ats_platform` |
| `redis-url` | agent, orchestrator | `redis://:PASS@redis-service.amol.svc.cluster.local:6379/1` |
| `jwt-secret` | agent, orchestrator | >= 32 random chars (user tokens + service-to-service tokens) |
| `llm-api-key` | agent, orchestrator | LM Studio accepts any value |
| `grafana-admin-user` | lgtm | Grafana admin login (edits dashboards, manages Grafana users) |
| `grafana-admin-password` | lgtm | strong password; without it the admin login stays locked |
| `grafana-viewer-user` | lgtm | Grafana read-only login for everyone else |
| `grafana-viewer-password` | lgtm | password of the read-only login |

Add the Grafana keys to the existing secret (values never go in the repo), then restart Grafana:

```bash
kubectl -n ats-platform patch secret ats-platform-secrets --type merge -p '{"stringData":{
  "grafana-admin-user":"<admin>","grafana-admin-password":"<strong password>",
  "grafana-viewer-user":"<viewer>","grafana-viewer-password":"<password>"}}'
kubectl -n ats-platform rollout restart deployment/ats-lgtm
```

## Middlewares (namespace ats-platform)

Managed by this repo (`base/routing/middlewares.yml`, names start with `ats-` so an apply never overwrites manually
created objects):

| Name | Purpose | Used by |
|---|---|---|
| `ats-security-headers` | nosniff, frame-deny, referrer policy, CSP that allows Next.js inline scripts and blob previews | ingress `ats-platform` |
| `ats-rate-limit` | 20 req/s average, burst 40, per client IP (depth 2) | ingress `ats-platform` |

Created manually in this namespace (copies of the `amol` ones) - **duplicates in purpose, not used by our routes**:

| Name | Duplicates | Why it is not used for /ats and /grafana |
|---|---|---|
| `security-headers` | `ats-security-headers` | CSP `default-src 'self'` blocks Next.js inline scripts and blob PDF previews (blank page); `sslRedirect` forces https |
| `rate-limit-2rps` | `ats-rate-limit` | 2 req/s, burst 1: one page load fetches many assets in parallel and gets 429 |
| `file-upload-limit` | upload limits in the app | `buffering` also buffers responses and breaks the streamed (SSE) answers; limits are enforced by the proxy (413) and the orchestrator (`FILE-LIM-0001`) |
| `zero-trust-auth` | JWT cookie auth | injects a static `x-api-key` header; it holds a plain-text key (move it to a Secret) and our APIs authenticate with JWT |
| `ats-root-redirect` | - | leftover from an earlier version (root redirect is no longer wanted): delete it |

Clean-up (optional, nothing references them):
```bash
kubectl -n ats-platform delete middlewares.traefik.io ats-root-redirect
kubectl -n ats-platform delete middlewares.traefik.io security-headers rate-limit-2rps file-upload-limit zero-trust-auth
kubectl -n ats-platform delete ingress ats-platform-root
```

## Shared infrastructure aliases

`base/dependencies/external-services.yml` creates `ExternalName` Services `postgres-service` and `redis-service`
in `ats-platform` that point to the services in `amol`, so the secret can keep short hostnames. The database name
comes from `DATABASE_NAME` in the ConfigMaps (`ats_platform`) and overrides the one in `database-url`.

## Deploy

```bash
./deploy.sh                      # schema job, then all services (dev overlay)
./deploy.sh schema               # re-run migrations / seed only
./deploy.sh orchestrator         # rebuild + roll out one service
```

The schema Job creates the `ats_platform` database if needed, runs `alembic upgrade head` and (dev) applies
`services/schema/seed/*.sql`. Job specs are immutable, so `deploy.sh` deletes the old Job before applying.

### Apply only the k8s changes (no build, no image change)

For YAML-only changes (ConfigMaps, ingress, middlewares, resources). The overlay uses the placeholder tag
`ats-<svc>:dev`, so swap in the images that are running now; otherwise the deployments would roll to a missing image.

```bash
OVERLAY=deploy/k8s/overlays/dev          # or deploy/k8s/overlays/prod
NS=ats-platform
SUBS=()
for svc in agent orchestrator frontend; do
  img=$(kubectl -n $NS get deployment ats-$svc -o jsonpath='{.spec.template.spec.containers[0].image}')
  SUBS+=(-e "s#image: ats-$svc:dev#image: $img#")
done

kubectl kustomize $OVERLAY | sed "${SUBS[@]}" | kubectl diff -f -     # preview what changes
kubectl kustomize $OVERLAY | sed "${SUBS[@]}" | kubectl apply -f -    # apply

# Pods read ConfigMaps only at start: restart after a config change
kubectl -n $NS rollout restart deployment/ats-agent deployment/ats-orchestrator deployment/ats-frontend
```

A single file works too, e.g. only the ingress: `kubectl -n ats-platform apply -f deploy/k8s/overlays/dev/ingress.yml`.
The schema Job is not part of the main overlay; run it with `./deploy.sh schema`.

## Access

Open http://amol.rackspire.ai/ats (UI) and http://amol.rackspire.ai/grafana (Grafana). No root route or redirect:
`/` and every other path stay on the existing ingresses in `amol`. Or port-forward:

```bash
kubectl -n ats-platform port-forward svc/ats-frontend 3000:80     # UI      http://localhost:3000/ats
kubectl -n ats-platform port-forward svc/ats-lgtm 3001:3000       # Grafana http://localhost:3001/grafana
```

Grafana → Dashboards → ATS → **ATS Platform Overview**: active users, running turns, queue, sign-ins, screenings,
turns per user (Loki), live user activity, latency, agent steps (agent vs fallback), LLM calls, cancellations,
errors by code, traces. The LGTM pod keeps data on an `emptyDir`; use a PVC for retention beyond pod restarts.
Grafana requires a sign-in (anonymous access is off; the image's default anonymous Admin is replaced by
`ats-grafana-start/run-grafana.sh`):

| login | role | can |
|---|---|---|
| `grafana-admin-user` | Admin | view and edit dashboards, manage Grafana users and data sources |
| `grafana-viewer-user` | Viewer | view dashboards, logs and traces only (no edits, no user management) |

Grafana data lives on an `emptyDir`, so both accounts are recreated from the secret on every pod start. Change a
password: update the secret, then `kubectl -n ats-platform rollout restart deployment/ats-lgtm`.
