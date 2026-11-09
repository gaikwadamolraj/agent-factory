# Shared host `amol.rackspire.ai` — gateway notes

The HTTP gateway (Traefik `IngressRoute`s + `Middleware`s) is now **part of the Helm chart**, created in the
**infra namespace** (`agent-infra`) by `templates/infra/routing/` and applied by `./deploy.sh`. So there is nothing
to apply here per deploy — except the one-time Traefik flag below.

## Routes the chart creates (host from `infra.routing.host`)

| Path | Backend (namespace) | Middlewares (agent-infra) |
|---|---|---|
| `/ats` (incl. `/ats/admin`) | `ats-ui` (agent-factory) | `security-headers`, `rate-limit` (+ stricter limits on login / chat-turn paths) |
| `/agent-factory` | `admin-ui` (agent-factory) | `security-headers`, `rate-limit` (+ stricter limit on login path) |
| `/grafana` | `lgtm` (agent-infra) | `grafana-security-headers`, `grafana-rate-limit` (`templates/infra/lgtm/route.yaml`) |

Everything else is internal (the UIs proxy to `ats-orchestrator`, `agent-factory`, `auth-service` in-cluster).
These prefixes are distinct from the existing `amol`-namespace ingresses (`/`, `/v1`, `/_next`, `/api`), so they
coexist without collision.

## One-time prerequisite: Traefik static config

The cluster's Traefik (kube-system) is outside the chart, so apply this yourself once:

```bash
kubectl apply -f deploy/review/amol-host/traefik-gateway-config.yaml
kubectl -n kube-system rollout status deploy/traefik
```

It enables (1) `allowCrossNamespace` — the IngressRoutes live in `agent-infra` but point at Services in
`agent-factory`; without it Traefik refuses the reference, the routers never register, and the UIs 404 or fall
through to whatever else claims the host — and (2) the **Coraza WAF plugin** used by the `waf` Middleware when
`infra.routing.waf.enabled=true`.

**CrowdSec is deliberately a separate file** (`traefik-crowdsec-config.yaml`). Its volumes have a hard
prerequisite: a **`crowdsec-bouncer` Secret** (key `api-key`) in **both** `kube-system` (the plugin's key file)
and `agent-infra` (so the LAPI registers the bouncer). Neither is created by the chart and the key is never
rendered into a manifest. When that Secret is missing the Traefik pod sits in `ContainerCreating` and the
*whole host* loses its routing — which is why the behavioural IPS must not be a prerequisite for having any
routing at all. Apply the CrowdSec file on top, after creating the Secret, only when turning CrowdSec on.
See docs/gateway_security.md §5c.

## Security layers (see docs/gateway_security.md for the full rules and the AWS/Azure mapping)

| Layer | Applies to | How to turn on |
|---|---|---|
| Security headers | all routes | on |
| Rate limits (general / auth / chat-turn / admin) | per route | on |
| WAF (OWASP CRS) | all exposed routes incl. the proxied chat API | plugin above + `infra.routing.waf.enabled=true` (starts DetectionOnly) |
| CrowdSec behavioural IPS | all exposed routes | `infra.crowdsec.enabled=true` + the static config above + the `crowdsec-bouncer` Secrets + `infra.routing.crowdsec.enabled=true` (docs/gateway_security.md §5c) |
| Admin IP allow-list | `/agent-factory` | fill `adminProtection.ipAllowList.allowedCIDRs`, set `enabled: true` |
| Admin basic-auth wall | `/agent-factory` | create the `admin-basic-auth` Secret in `agent-infra`, set `enabled: true` |
| HTTPS-only | `/agent-factory` | already (websecure entry point only) |
