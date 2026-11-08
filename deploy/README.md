# Deployment

One Helm chart, [helm/agent-factory](helm/agent-factory/README.md), deployed only with `./deploy.sh` (release `ats`).

| Namespace | What runs there |
|---|---|
| `agent-factory` | ATS services `auth-service`, `ats-orchestrator`, `agent-skill`, `agent-compute`, `agent-sandbox`, `agent-knowledge-graph`, `agent-knowledge-graph-worker`, `agent-guardrail`, `ats-ui`; schema Job; UI routing; KEDA ScaledObject; aliases `postgres-service` / `redis-service` |
| `agent-infra` | shared infrastructure: `postgres`, `redis` (one Redis 8 for everything), `falkordb`, `llm-gateway`, `lgtm` (Grafana under `/grafana`) |

```
deploy/
├── helm/agent-factory/   # the chart: infra per-component (templates/infra), backends as values blocks rendered by
│                        #   templates/services/{backend,config}.yaml, routing in templates/routing, values-<env>.yaml
├── compose/             # docker compose extras (Grafana dashboard provider)
└── review/              # routes for shared hosts, applied by hand after review
```

## New machine

```bash
./deploy.sh init --generate-secrets   # namespaces agent-factory + agent-infra, generates the random keys of
                                     # agent-factory-secrets (signing key, client secrets; never printed),
                                     # lists the other keys to create (and which are missing)
# create agent-factory-secrets (agent-factory) and agent-infra-secrets (agent-infra) from
#   helm/agent-factory/files/secret.example.agent-factory.yaml / secret.example.agent-infra.yaml
./deploy.sh          # builds every image, installs KEDA if missing, helm upgrade --install
```

`./deploy.sh` stops before building when a Secret or required key is missing, or when a value that must be shared
differs between the two Secrets (`postgres-user`, `postgres-password`, `falkordb-password`,
`llm-gateway-key` = `master-key`). It never prints Secret values.

Required keys:

| Secret | Keys | How to get them |
|---|---|---|
| `agent-factory/agent-factory-secrets` | `auth-signing-key` | generated: EC P-256 PEM (`openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256`) |
| | `client-secret-ats-orchestrator`, `client-secret-agent-knowledge-graph` | generated: `openssl rand -hex 32` (one per caller in `services.authService.oauthClients`) |
| | `database-url`, `redis-url`, `llm-gateway-key`, `falkordb-password`, `postgres-user`, `postgres-password` | yours (see the secret example) |
| | optional: `auth-signing-key-previous` (key rotation), `hf-key` | |
| `agent-infra/agent-infra-secrets` | `postgres-user`, `postgres-password`, `postgres-db`, `redis-password`, `falkordb-password`, `master-key`, `lmstudio-api-key`, `vllm-qwen3-4b-api-key` | yours; optional `grafana-*` |

`./deploy.sh init --generate-secrets` only adds missing generated keys (it never overwrites one) and creates
`agent-factory-secrets` if it does not exist yet. Removed keys: `jwt-secret` (replaced by the auth-service keys),
`knowledge-redis-password` (graph-redis merged into `redis`); delete them from existing Secrets when convenient.
Signing-key rotation: [docs/security_auth.md](../docs/security_auth.md).

## Everyday use

```bash
./deploy.sh                                  # build everything, deploy
./deploy.sh auth-service ats-orchestrator  # build these, deploy; the rest keep their running tags
./deploy.sh apply                            # no build: chart/config changes (models, ConfigMaps, routes)
./deploy.sh schema                           # new schema image; the migration hook runs on every deploy
./deploy.sh keda                             # install / upgrade the KEDA operator (Helm chart kedacore/keda)
ENV=prod REGISTRY=registry.example.com/ats ./deploy.sh   # values-prod.yaml, images pushed to REGISTRY
```

Components: `auth-service ats-orchestrator agent-skill agent-compute agent-sandbox agent-knowledge-graph
agent-guardrail frontend schema` (`agent-knowledge-graph` also covers the worker). Tags of components that are not rebuilt are
read from ConfigMap `image-tags`, which the chart writes on every release, so a partial deploy never rolls
another service to a missing image. Dev images go into k3d (`CLUSTER_NAME`, default `my-cluster`).

One-time switch from the old kustomize objects: `./deploy.sh migrate-to-helm --dry-run`, then without
`--dry-run` ([scripts/cutover_to_helm.sh](../scripts/cutover_to_helm.sh): backup YAML to `backups/`, Secret check,
delete the old objects, full deploy).

## Access

http://amol.rackspire.ai/ats (UI) and http://amol.rackspire.ai/grafana (Grafana). Or port-forward:

```bash
kubectl -n agent-factory port-forward svc/ats-ui 3000:80   # UI      http://localhost:3000/ats
kubectl -n agent-infra port-forward svc/lgtm 3001:3000          # Grafana http://localhost:3001/grafana
```

Grafana requires a sign-in: `grafana-admin-user` (Admin) and `grafana-viewer-user` (Viewer) from
`agent-infra-secrets`; both are recreated from the Secret on every pod start (data is on an `emptyDir`). Change a
password: update the Secret, then `kubectl -n agent-infra rollout restart deployment/lgtm`.
Dashboards → ATS → **ATS Platform Overview**.

Changing `falkordb-password` (or another shared password): update it in both Secrets, then restart the infra pod and
its clients, e.g. `statefulset/falkordb` (agent-infra) and `deployment/agent-knowledge-graph`,
`deployment/agent-knowledge-graph-worker` (agent-factory).

## Middlewares in agent-factory not managed by the chart

Created by hand earlier, unused by our routes: `security-headers` (CSP blocks Next.js inline scripts),
`rate-limit-2rps` (too strict for page loads), `file-upload-limit` (buffering breaks SSE), `zero-trust-auth`
(static key in plain text). Optional clean-up:
`kubectl -n agent-factory delete middlewares.traefik.io security-headers rate-limit-2rps file-upload-limit zero-trust-auth`.
