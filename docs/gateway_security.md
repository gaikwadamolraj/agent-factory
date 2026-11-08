# Gateway security

Every request from the internet enters through one gateway (Traefik today) before it reaches any service. This doc
is the single reference for the gateway rules, and it is written to be **portable**: each rule maps to an AWS or
Azure equivalent (§6), so the same protection travels with the product to any cloud.

Principle: **defense in depth**. The gateway is the first wall, not the only one — every service still verifies the
JWT itself (JWKS), enforces default-deny IAM policy, and checks tenant scope. A gap in one layer is covered by the
next.

## 1. Where the rules live

All gateway objects are created by the Helm chart in the **infra namespace** (`agent-infra`), so routing is in one
place and deployed automatically:

| File | What |
|---|---|
| `deploy/helm/agent-factory/templates/infra/routing/ingressroutes.yaml` | one IngressRoute per exposed backend; builds the middleware chain |
| `deploy/helm/agent-factory/templates/infra/routing/middlewares.yaml` | the Middlewares (headers, rate limits, WAF, IP allow-list, basic-auth) |
| `deploy/helm/agent-factory/values.yaml` → `infra.routing` | all the knobs (host, limits, WAF directives, admin protection) |
| `deploy/review/amol-host/traefik-gateway-config.yaml` | Traefik static config (cross-namespace + WAF plugin) — applied by hand once |

The backends run in `agent-factory`; the IngressRoutes reference them cross-namespace, which is why Traefik needs
`allowCrossNamespace=true` (the review file).

## 2. Routes (exposed surface)

Nothing is exposed unless its values block declares a `route`. Host comes from `infra.routing.host`.

| Path | Backend | Notes |
|---|---|---|
| `/ats` (incl. `/ats/admin`) | `ats-ui` | recruiter UI; the chat API is proxied under here (`/ats/api/backend/...`) |
| `/agent-factory` | `admin-ui` | admin console — the hardened route (§4) |
| `/grafana` | `lgtm` | observability |

Everything else (`ats-orchestrator`, `agent-factory`, `auth-service`, the agents) is cluster-internal; the UIs proxy
to them server-side, so no API and no token is ever exposed to the browser.

## 3. Rules on every route

1. **Security headers** (`security-headers`): `contentTypeNosniff`, `frameDeny`, `browserXssFilter`,
   `referrerPolicy`, and a strict `contentSecurityPolicy`.
2. **Rate limits** (per client IP, `ipStrategy.depth` accounts for proxies in front of Traefik):
   - `rate-limit` — general UI traffic.
   - `rate-limit-auth` — login/refresh/logout (brute force; the app also locks a username after 5 failures).
   - `rate-limit-turns` — sending a chat turn (protects LLM cost).
   - `rate-limit-admin` — the admin console (deliberately tight).
3. **WAF** (§5) — on every exposed route when enabled.

## 4. The admin console is extra-protected (`/agent-factory`)

On top of the app's auth, the gateway adds (each toggVle in `infra.routing.adminProtection`):

- **IP allow-list** (`admin-ip-allowlist`): only listed CIDRs (office/VPN) can reach `/agent-factory` at all;
  everyone else gets 403 before the app. Fill `allowedCIDRs`, set `enabled: true` (empty + enabled would lock you
  out, so it ships off).
- **Basic-auth wall** (`admin-basic-auth`): a second credential prompt in front of the console login, from an
  htpasswd Secret in `agent-infra`. A stolen app session still hits this wall first.
- **Tight rate limit** (`rate-limit-admin`): always on for this route.
- **HTTPS-only**: the route uses the `websecure` entry point only.

A route opts in with `route.admin: true`; the gateway then prepends the enabled admin middlewares to every rule.

## 5. WAF (OWASP Core Rule Set)

Open-source Traefik v3 runs a WAF through the **Coraza WASM plugin** loaded in static config; the chart's `waf`
Middleware references it and is applied to **every exposed route**, so the proxied **chat API** (under `/ats`) is
inspected too.

- Config: `infra.routing.waf.directives`. Ships in **`SecRuleEngine DetectionOnly`** — it logs would-be blocks
  without refusing traffic, so you tune out false positives first, then switch to `SecRuleEngine On`.
- **Response-body inspection is OFF** (`SecResponseBodyAccess Off`) on purpose: the chat answers stream over SSE,
  and buffering responses would break them. Request inspection stays on.
- `crsEnabled` mounts the embedded OWASP CRS (SQLi, XSS, path traversal, RCE, scanner/bot signatures, protocol
  anomalies). Turn on with `infra.routing.waf.enabled: true` after the plugin is loaded (review file).

### SQL injection — how it is actually prevented

The WAF catches SQLi *payloads* as a bonus, but the real defense is in the app and is always on:
- Every query goes through SQLModel/SQLAlchemy repositories with **bind parameters** (asyncpg `$1, $2, …`); values
  are never concatenated into SQL (CLAUDE.md §8: no raw SQL in handlers).
- The one raw statement in the codebase (the RLS tenant GUC) is parameterized (`set_config(:guc, :value, true)`).
- PgBouncer's `statement_cache_size=0` disables *caching* of prepared statements, **not** parameterization, so it
  does not weaken SQLi protection.

## 5b. Bot / AI-scraper defense and behavioural IPS

Two more gateway layers, both plugins loaded in Traefik static config (review file), applied to every exposed route,
default off:

- **Bot Wrangler** (`bot-defense`, `infra.routing.botDefense`): checks traffic against known AI-bot / scraper
  lists and `log`s, `block`s (403) or tarpits them — keeps LLM-scraping agents off the chat and the UI.
- **CrowdSec** (`crowdsec`, `infra.routing.crowdsec`): behavioural IPS. The **bouncer** middleware asks the
  CrowdSec **LAPI** whether a client IP is banned; bans are decided by a CrowdSec **agent** reading Traefik access
  logs for patterns (404 scanning, credential stuffing, scraping). The LAPI + agent are a dedicated infra component
  (`infra.crowdsec`, §5c); the bouncer plugin and the component are both default-off. Turn-on needs both of them, the
  Traefik static-config changes (§5c) and a bouncer API-key Secret.

Order on each route: CrowdSec (IP ban) → Bot defense → WAF → (admin chain) → security headers → rate limit.

## 5c. CrowdSec behavioural IPS — the full picture

CrowdSec has three parts: the **LAPI** (decision store + API), the **agent** (reads logs, runs scenarios, writes
decisions to the LAPI) and the **bouncer** (the Traefik plugin that enforces them). They are wired like this:

```
internet → Traefik ──(crowdsec Middleware/bouncer plugin asks: is this IP banned?)──→ CrowdSec LAPI (agent-infra:8080)
             │                                                                              ▲
             └── writes JSON access log to /var/log/traefik/access.log (node hostPath) ──→ CrowdSec agent (reads it)
```

**Topology chosen (single-node k3d): one shared log file on the node.** The LAPI and the agent run in **one pod**
(`templates/infra/crowdsec/`, the image default runs both roles). Traefik (in `kube-system`) is configured to write
a **JSON access log to a file on a node path** (`/var/log/traefik/access.log`); the CrowdSec pod mounts that **same
host path read-only** (`hostPath`) and acquires it (an `acquis.d` ConfigMap labels the lines `type: traefik`, routed
to the `crowdsecurity/traefik` parsers/scenarios installed via the `COLLECTIONS` env). The LAPI's SQLite DB is on a
PVC (required since CrowdSec 1.7.0). A `crowdsec` Service exposes the LAPI on **8080**, so the bouncer's
`lapiUrl` (`http://crowdsec.agent-infra.svc.cluster.local:8080`) resolves.

*Why this topology:* it is the simplest that works on one node and needs no CRI-log parsing or privileged container
log access — Traefik writes a plain file, CrowdSec reads a plain file.

**Limitations (validate / revisit on a real cluster):**
- **Single-node only.** `hostPath` is node-local: on a multi-node cluster Traefik and the CrowdSec agent can land on
  different nodes and the agent sees an empty file. A multi-node setup needs the agent as a **DaemonSet reading each
  node's container logs** (the upstream CrowdSec Helm chart pattern) instead of this shared file.
- **No HA.** One LAPI pod, `Recreate` strategy, RWO PVC — fine for a gateway sidecar, not for HA.
- **Container user.** The upstream image generates config under `/etc/crowdsec` and writes the DB; it runs as root by
  default. The PVC uses `fsGroup`; running fully non-root (and read access to the root-owned access-log file) needs
  verification on a live cluster.

### How the bouncer API key is supplied safely

The bouncer plugin runs **inside the Traefik pod in `kube-system`** — a namespace the chart must never touch — so the
key cannot come from a chart Secret reference. The plugin supports reading the key **from a file**
(`crowdsecLapiKeyFile`), so:

- The chart's `crowdsec` Middleware renders **only the file path** (`infra.routing.crowdsec.apiKeyFile`, default
  `/etc/traefik/crowdsec/api-key`) — **never the key value**.
- The Traefik pod mounts a Secret **`crowdsec-bouncer`** (key `api-key`) at that path. This mount is added in the
  hand-applied static config (`deploy/review/amol-host/traefik-gateway-config.yaml`); the Secret must be created **in
  `kube-system`** by hand.
- The LAPI side registers the same key: the CrowdSec pod reads it from a Secret (`infra.crowdsec.bouncerKeySecret`,
  default the same `crowdsec-bouncer`/`api-key`) as `BOUNCER_KEY_traefik-bouncer`, so the LAPI auto-registers that
  bouncer with that key on start. The env ref is **optional** — if the Secret is absent the pod still starts and you
  register by hand.

So the key value lives only in Secrets (one in `agent-infra` for the LAPI, one in `kube-system` for Traefik) and is
never written into any rendered manifest, per CLAUDE.md §4/§7.

*Fallback:* set `infra.routing.crowdsec.apiKeyFile: ""` and the Middleware renders without a key; you then patch the
Middleware's `plugin.crowdsec.crowdsecLapiKey` with the key yourself (inline). Not recommended — it puts the key in a
live object.

### Generate / register the key

```bash
# After infra.crowdsec is deployed, generate a bouncer + key on the LAPI pod (prints the key once):
kubectl -n agent-infra exec deploy/crowdsec -- cscli bouncers add traefik-bouncer
# Put that key where both sides read it:
#   LAPI side (auto-register on next start):
kubectl -n agent-infra create secret generic crowdsec-bouncer --from-literal=api-key='<KEY>'
#   Traefik side (the plugin's key file):
kubectl -n kube-system create secret generic crowdsec-bouncer --from-literal=api-key='<KEY>'
```

If you pre-create the `agent-infra` Secret *before* first start, the `BOUNCER_KEY_traefik-bouncer` env registers the
bouncer for you and you can skip `cscli bouncers add`.

### Turn CrowdSec on

1. `infra.crowdsec.enabled: true` (deploys the LAPI + agent, Service, PVC, acquisition ConfigMap, NetworkPolicy).
2. Apply the Traefik static config (§5c mounts + plugin + access log): re-apply
   `deploy/review/amol-host/traefik-gateway-config.yaml` and roll Traefik.
3. Create the `crowdsec-bouncer` Secret in **both** `agent-infra` and `kube-system` (above).
4. `infra.routing.crowdsec.enabled: true` (adds the `crowdsec` bouncer Middleware to every exposed route), redeploy
   with `./deploy.sh`.

## 6. Cloud portability — the same rules on AWS / Azure

The gateway is deliberately made of standard pieces, so each rule has a managed equivalent. When the product runs on
a cloud, implement the same table with that cloud's gateway + WAF instead of Traefik; the services and their
app-level auth do not change.

| Gateway rule (here) | AWS | Azure |
|---|---|---|
| Ingress / routing + TLS | ALB (Ingress) or API Gateway; ACM cert | Application Gateway or Front Door; App Gateway listener cert |
| WAF (OWASP CRS) | **AWS WAF** web ACL with the AWS Managed Rules **Core rule set** (+ SQLi/known-bad-inputs), attached to the ALB/API GW/CloudFront | **Azure WAF** policy with the **OWASP CRS** managed ruleset, on Application Gateway WAF_v2 or Front Door |
| Rate limits (general/auth/turns/admin) | AWS WAF **rate-based rules** (per path/IP) | Azure WAF **rate-limit custom rules** (per match + IP) |
| Behavioural IPS / IP reputation (CrowdSec) | AWS WAF **rate-based rules** + **Amazon IP reputation** / **Anonymous IP** managed rule groups (+ **Shield** for DDoS) | **Azure WAF** rate-limit + **bot-protection** managed ruleset on **Front Door** (+ Azure DDoS Protection) |
| Admin IP allow-list | AWS WAF **IPSet** match (allow) + default block on the `/agent-factory` path, or ALB SG/path rule | Azure WAF **custom rule** matching `RemoteAddr` IP ranges on the admin path |
| Admin basic-auth wall | CloudFront/ALB + Lambda@Edge / OIDC on the listener, or API GW authorizer | App Gateway/Front Door rule or an auth proxy; or AAD on the admin path |
| Security headers | CloudFront response-headers policy / ALB rules | Front Door rules set / App Gateway rewrite rules |
| HTTPS-only | ALB HTTP→HTTPS redirect; HSTS | App Gateway/Front Door redirect; HSTS |
| DDoS / edge | AWS Shield (+ CloudFront) | Azure DDoS Protection (+ Front Door) |
| Secrets for the above | AWS Secrets Manager / ACM | Azure Key Vault |

Two rules that stay in the app regardless of cloud (a WAF does not replace them): **authentication** (every service
verifies the ES256 JWT via JWKS) and **authorization** (default-deny IAM policy + tenant/RLS). The WAF filters
malicious *payloads*; it never decides *who may do what* — that is the app's job (docs/security_auth.md).

## 7. Turn-on checklist

1. Apply the Traefik static config once: `kubectl apply -f deploy/review/amol-host/traefik-gateway-config.yaml`.
2. WAF: set `infra.routing.waf.enabled: true` (stays DetectionOnly), deploy, watch Traefik logs for CRS hits, then
   change the first directive to `SecRuleEngine On`.
3. Admin IP allow-list: put your CIDRs in `adminProtection.ipAllowList.allowedCIDRs`, set `enabled: true`.
4. Admin basic-auth: create the Secret, set `adminProtection.basicAuth.enabled: true`:
   `htpasswd -nbB admin '<pw>' | kubectl -n agent-infra create secret generic admin-basic-auth --from-file=users=/dev/stdin`
5. CrowdSec (behavioural IPS): follow §5c — `infra.crowdsec.enabled: true`, re-apply the Traefik static config (plugin
   + JSON access log + mounts), create the `crowdsec-bouncer` Secret in both `agent-infra` and `kube-system`, then
   `infra.routing.crowdsec.enabled: true`.
6. Redeploy with `./deploy.sh`.
