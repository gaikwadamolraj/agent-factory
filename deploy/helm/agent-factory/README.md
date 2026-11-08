# agent-factory Helm chart

The only deploy path: one release (`ats`) with the ATS services and the shared infrastructure they need, installed
by `./deploy.sh` (see [deploy/README.md](../../README.md)) with `values.yaml` + one environment file.

## Namespaces and names

| Namespace | Holds | Naming |
|---|---|---|
| `agent-factory` (release namespace) | ATS services, their ConfigMaps, `llm-config`, `auth-config`, routing of the UI, KEDA ScaledObject, schema Job | services = their folder name (`agent-skill`, ...); product-wide objects keep `ats-` (`ats-ui`, `llm-config`, `schema-*`, `image-tags`) |
| `agent-infra` (`infra.namespace`) | `postgres`, `redis` (one Redis 8 for everything), `falkordb`, `llm-gateway`, `lgtm` (+ Grafana route) | the component name only, no product prefix: shared platform infrastructure |

Both Namespace objects are part of the chart with `helm.sh/resource-policy: keep` (uninstall never deletes them or
their Secrets). Every object sets `metadata.namespace` explicitly (`ats.metadata` takes an optional `namespace`;
`ats.infraMetadata` / `ats.infraService` put an object into `infra.namespace`). Apps reach infra services by FQDN
(`http://llm-gateway.agent-infra.svc.cluster.local/v1`, `falkordb.agent-infra...`,
OTLP `http://lgtm.agent-infra.svc.cluster.local:4318`); values may use `{{ .Values.infra.namespace }}` for that
(ConfigMap data and plain env values go through `tpl`).

Application backends are not per-service files: each is a values block (`services:` public-capable, `agents:`
internal), and two generic templates render them all. Add a backend by copying a values block - no new template.

```
templates/
├── _helpers.tpl                      # labels, metadata, image, env, ConfigMap, Service, NetworkPolicy, Deployment,
│                                     #   configData, configChecksums, serviceName, networkPolicy
├── release/image-tags.yaml           # ConfigMap image-tags: tag of every ATS image (read by deploy.sh)
├── services/
│   ├── backend.yaml                  # THE backend template: ranges services + agents -> Deployment (+ Service),
│   │                                 #   NetworkPolicy and PVCs for every enabled block
│   └── config.yaml                   # THE config template: ConfigMap <name>-config per block + shared auth-config
├── routing/
│   ├── routes.yaml                   # per-backend Traefik IngressRoute for blocks with a `route` (only the UI)
│   └── middlewares.yaml              # shared Middlewares: security-headers + the rate limits
├── infra/
│   ├── namespaces/                   # Namespaces agent-factory + agent-infra (kept on uninstall)
│   ├── postgres/  redis/             # agent-infra: StatefulSet + Service, NetworkPolicy
│   ├── falkordb/                     # agent-infra: StatefulSet + Service, NetworkPolicy
│   ├── llm-gateway/                  # agent-infra: ConfigMaps, Deployment + Service, NetworkPolicy
│   ├── lgtm/                         # agent-infra: ConfigMaps, Deployment + Service, route (Middlewares + Ingress)
│   ├── llm-config/                   # agent-factory: ConfigMap llm-config
│   └── keda/                         # agent-factory: ScaledObject + TriggerAuthentication of the worker
└── jobs/schema-job.yaml              # migration Job: hook post-install,pre-upgrade
files/
├── litellm-config.yaml               # THE model configuration of the LLM gateway (docs/llm_gateway.md)
├── dashboards/*.json                 # Grafana dashboards
├── grafana/run-grafana.sh
└── secret.example.<namespace>.yaml   # documentation of the two Secrets (never rendered)
```

## Secrets (created by hand, never part of the chart)

The chart never renders a Secret. "The secrets template" is two things: (1) every secret value is referenced
uniformly from a block's `env` as `secretKeyRef: {secret: platform|infra, key, optional}` (helper `ats.env`), so the
chart only ever names an existing Secret and its key; and (2) `files/secret.example.<namespace>.yaml` is the single
source of the required keys. The table below lists them.

| Secret | Namespace | Required keys |
|---|---|---|
| `agent-factory-secrets` (`secrets.platform`) | agent-factory | `database-url`, `redis-url`, `auth-signing-key`, `client-secret-ats-orchestrator`, `client-secret-agent-knowledge-graph`, `llm-gateway-key`, `falkordb-password`, `postgres-user`, `postgres-password` (optional `auth-signing-key-previous`, `hf-key`) |
| `agent-infra-secrets` (`secrets.infra`) | agent-infra | `postgres-user`, `postgres-password`, `postgres-db`, `redis-password`, `falkordb-password`, `master-key`, `lmstudio-api-key`, `vllm-qwen3-4b-api-key` (optional `grafana-admin-user`, `grafana-admin-password`, `grafana-viewer-user`, `grafana-viewer-password`) |

Same value in both: `postgres-user`, `postgres-password`, `falkordb-password`, and `llm-gateway-key` = `master-key`.
Generated keys (`auth-signing-key`, `client-secret-*`): `./deploy.sh init --generate-secrets` creates the missing ones. `./deploy.sh` checks keys and equality (without printing values) before it
deploys. Templates: [files/secret.example.agent-factory.yaml](files/secret.example.agent-factory.yaml),
[files/secret.example.agent-infra.yaml](files/secret.example.agent-infra.yaml). In values, an env entry names a
Secret by key: `secretKeyRef: {secret: platform|infra, key: ..., optional: ...}`.

## How it works

- **Images**: ATS images `<name>:<tag>` from `global.image.registry` (empty = local k3d images); the tag is
  `services.<x>.image.tag`, else `global.image.tag`, else `appVersion`. `image-tags` records the tag of every
  component, so `deploy.sh` can rebuild one service and keep the running tags of the others.
- **ConfigMaps** have stable names; consumers carry `checksum/config` (and `checksum/llm-config`) pod annotations,
  so a config change rolls exactly the pods that read it.
- **Schema migration** (`jobs/schema-job.yaml`): `schema-config` + Job `schema-migrate` are hooks
  `post-install,pre-upgrade` with `before-hook-creation`. Install: runs once Postgres (part of this release) and the
  services are ready (no service health check touches tables). Upgrade: runs and must succeed before any service is
  updated. Logs: `kubectl -n agent-factory logs job/schema-migrate`.
- **NetworkPolicies**: infra policies admit pods of the release namespace by `namespaceSelector`
  (`kubernetes.io/metadata.name: agent-factory`) plus a pod selector: `postgres` (all ATS pods + namespace `keda`),
  `redis` (all ATS pods), `falkordb` (component `knowledge-graph`), `llm-gateway`
  (`infra.llmGateway.callers`). In agent-factory: `agent-knowledge-graph` (orchestrator only), `agent-sandbox` (compute
  only, no egress), `agent-guardrail` (orchestrator + knowledge graph; egress DNS, 443 for the model download,
  auth-service, OTLP), `auth-service` (ingress from agent-factory pods; egress DNS, Postgres, Redis, OTLP).
- **Routing (expose nothing by default)**: a backend is reachable from outside ONLY if its values block declares a
  `route` with `enabled: true`; `templates/routing/routes.yaml` then emits one Traefik IngressRoute for it.
  Only `atsUi` has a route (the UI catch-all on the shared host plus two higher-priority rules with the stricter
  auth / chat-turn rate limits). The Middlewares it references are shared (`templates/routing/middlewares.yaml`).
- **Grafana route**: Ingress `grafana` + Middlewares `grafana-security-headers` / `grafana-rate-limit` live in
  agent-infra, because Traefik does not follow cross-namespace middleware references.
- **KEDA** (`keda.enabled`, on in dev and prod): ScaledObject + TriggerAuthentication in agent-factory next to the
  worker; the worker Deployment then has no `replicas`. The operator itself is a separate Helm release
  (`./deploy.sh keda`, chart `kedacore/keda` pinned in deploy.sh).
- **agent-guardrail**: model cache on PVC `agent-guardrail-models` (3Gi, kept on uninstall); a startup probe
  gives the first model download up to 10 minutes; `/healthz` is 503 until the classifier is loaded.

## Commands

```bash
CHART=deploy/helm/agent-factory
helm lint --strict $CHART -f $CHART/values.yaml -f $CHART/values-dev.yaml
helm template ats $CHART -n agent-factory -f $CHART/values.yaml -f $CHART/values-dev.yaml --set global.image.tag=x
```

Deploy only with `./deploy.sh` (it sets every image tag and checks Secrets first).

## Add a service

No new template - just a values block (copy a similar one). Put it under `services:` if it is public-capable
(product / platform), or `agents:` if it is an internal agent. The object/Service/DNS name is the block key in
kebab-case (`atsUi` -> `ats-ui`), or its `name`.

1. Block: `enabled`, `replicas`, `image.repository`, `containerName`, `ports`, `service.ports` (omit `service` for a
   workload with no Service), `config`, `envFrom`, `env`, probes, `resources`, `podSecurityContext`.
   - `envFrom` lists the ConfigMaps it reads; each adds a `checksum/*` annotation so the pod rolls on change. Every
     service lists `auth-config` (it verifies tokens with the JWKS).
   - `networkPolicy` to restrict who may reach it / what it may reach; `volumeClaims` for a kept PVC.
   - `route` (host/path/middlewares/entryPoints) ONLY if it must be reachable from outside (expose nothing by default).
2. LLM user: add `<name>` to `infra.llmGateway.callers` and `llm-config` to its `envFrom`.
   Calls other services: add it to `services.authService.oauthClients` (see Auth).
3. deploy.sh: add it to `COMPONENTS`, `tag_key`, `service_dir`/`tag_value_path` (`services.*` vs `agents.*`), and
   `build_image` if its build context differs.

Placeholders `services.adminUi` and `agents.agentFactory` (both `enabled: false`) show where a future UI / agent
goes; their images do not exist yet, so they render nothing until filled in and enabled.

## Add an infrastructure component

`infra.<component>` values with `enabled` and a pinned `image`; `templates/infra/<component>/` using
`ats.infraMetadata`, `ats.infraService`, `ats.callersNetworkPolicy`; secrets only from `secrets.infra`.

## Auth

- `services.authService` (block): Deployment + Service + NetworkPolicy via `backend.yaml`; ConfigMap
  `auth-service-config` (with `OAUTH_CLIENT_AUDIENCES` rendered from `oauthClients`) via `config.yaml`, which also
  renders the shared ConfigMap `auth-config` (`authConfig`: `AUTH_ISSUER`, `AUTH_JWKS_URL`, `AUTH_TOKEN_URL`) in
  the envFrom of every service.
- Each caller in `oauthClients` gets `SERVICE_CLIENT_SECRET` from `client-secret-<caller>`; the auth-service gets
  `CLIENT_SECRET_<CALLER>` from the same key. Add a caller: list it in `oauthClients`, add the env entry to its
  values, add the key to `CLIENT_SECRET_KEYS` in deploy.sh, run `./deploy.sh init --generate-secrets`.
- No Traefik forwardAuth: services verify tokens themselves (docs/security_auth.md).

## Redis

One Redis 8 (`infra.redis`): AOF on, `maxMemory` with `maxMemoryPolicy: volatile-lru` (only keys with a TTL are
evicted; version counters have none). The knowledge graph uses database 0 (`REDIS_DB: "0"`) for its vector-search
semantic cache; the other services use the database in `redis-url`.
