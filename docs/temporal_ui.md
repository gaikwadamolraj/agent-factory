# The Temporal UI

What it shows, why it is off, and how to turn it on. Checked against the chart and the live cluster on
9 November 2026.

---

## Is it enabled? No.

Three separate switches, all `false` in `values.yaml`, and `values-dev.yaml` overrides none of them:

| Switch | Default | Creates |
|---|---|---|
| `infra.temporal.enabled` | `false` | the Temporal **server** (`temporalio/auto-setup:1.26.2`) + its Service and NetworkPolicy |
| `infra.temporal.ui.enabled` | `false` | the **web UI** (`temporalio/ui:2.34.0`) + its Service |
| `infra.temporal.ui.route.enabled` | `false` | the **`/temporal` Ingress** on `amol.rackspire.ai` |

Confirmed on the cluster: `kubectl get pods -n agent-infra` lists falkordb, lgtm, llm-gateway, postgres and
redis — **no temporal**. So `https://amol.rackspire.ai/temporal` is not served by this chart today.

They are three switches rather than one on purpose: you can run the engine without exposing its UI, which
is the right shape for production. The UI shows run arguments, results and stack traces **across every
tenant**.

---

## What it would show you

The engine's own view of the durable journeys — the part `workflow_runs` deliberately does *not* store:

- **Every execution** of `onboard_subject` (J1) and `bulk_match` (J3), by workflow id
  (`onboard_subject-c1-ats-candidate-document-42`).
- **The full event history** — each activity attempt, its input, its result, its retries and backoff.
  This is what `workflow_runs` is a summary of.
- **Why something is stuck** — a pending activity, a worker that never picked the task up, an exhausted
  retry policy.
- **Queries** against a running workflow: `stage` on J1, `progress` on J3 (selected / graded / failed).
- **Task queues** — `onboarding` and `bulk-match`, and whether anything is polling them.

For day-to-day questions ("did this candidate get onboarded?") use `GET /api/v1/runs` instead: it answers
from our own projection and does not need the engine to be reachable. Reach for the Temporal UI when you
need to know *why* a run behaved the way it did.

---

## Turning it on

### 1. Enable the three switches

Add to `deploy/helm/agent-factory/values-dev.yaml`:

```yaml
infra:
  temporal:
    enabled: true
    ui:
      enabled: true
      route:
        enabled: true
```

### 2. Deploy

```bash
./deploy.sh apply
```

No image build is needed — Temporal and its UI are upstream images. The server runs `auto-setup`, which
creates its two databases (`temporal`, `temporal_visibility`) in the shared Postgres on first start and is
idempotent afterwards. Our schema job does not create them.

### 3. Check it came up

```bash
kubectl get pods -n agent-infra | grep temporal
kubectl get ingress temporal-ui -n agent-infra
```

Then open **`https://amol.rackspire.ai/temporal`**.

> The host sits behind Cloudflare, which currently returns 403 to requests that do not satisfy its WAF rule.
> If the page is blocked there, port-forward instead:
> `kubectl port-forward -n agent-infra svc/temporal-ui 8088:8080` → `http://localhost:8088/temporal`.

---

## ⚠ Read this before exposing it

`infra.routing.adminProtection` has **both protections off by default**:

```yaml
adminProtection:
  ipAllowList:
    enabled: false
  basicAuth:
    enabled: false
```

The route template adds the IP allow-list and basic-auth middlewares *only when those are enabled*. With
both off, the rendered Ingress carries just `temporal-security-headers` and `temporal-rate-limit` — so
**anyone who can reach the host can read every tenant's run arguments and results.**

Turn at least basic auth on before exposing it:

```bash
htpasswd -nbB admin '<password>' \
  | kubectl -n agent-infra create secret generic admin-basic-auth --from-file=users=/dev/stdin
```

```yaml
infra:
  routing:
    adminProtection:
      basicAuth:
        enabled: true
        secret: admin-basic-auth
```

Add the IP allow-list too if you have stable office or VPN ranges — but fill the CIDRs *before* setting
`enabled: true`, or you lock yourself out of `/agent-factory` as well (the same block guards both).

---

## It will look empty, and that is correct

Enabling Temporal does **not** start any workflows. Two more things are needed:

1. **`TEMPORAL_ADDRESS` reaches the orchestrator automatically.** The chart renders it only when
   `infra.temporal.enabled` is true, so turning the engine on is what makes the producers start launching
   runs instead of falling back to the in-process path. No second setting to remember.

2. **The workers are separate Deployments, and they are off.** `agents.atsOrchestratorOnboarding` and
   `agents.atsOrchestratorBulkMatch` both default to `enabled: false`. Without them, runs are *accepted*
   and sit in their queue with nothing polling — visible in the UI as workflows with a pending first task,
   which is a useful thing to be able to see.

```yaml
agents:
  atsOrchestratorOnboarding:
    enabled: true
  atsOrchestratorBulkMatch:
    enabled: true
```

Each worker needs its own client-credentials secret (`client-secret-ats-orchestrator-onboarding`,
`client-secret-ats-orchestrator-bulk-match`); `./deploy.sh init --generate-secrets` creates them.

KEDA then scales each worker on its own queue's backlog, read from our `workflow_runs` table rather than
from Temporal — so KEDA needs no engine credentials.

---

## How the two views relate

| | Temporal | `workflow_runs` |
|---|---|---|
| Owns | execution: history, replay, retries, timers | the product's record: which runs exist, what they were about, how they ended |
| Survives | a namespace rebuild loses it | outlives the engine entirely |
| Read by | a human debugging one run | the API, the UI, an audit |
| Reachable | only when the engine is up | always |

Both are written deliberately: `JourneyLauncher` writes the row on the way in (only after the engine accepts
the run), the workflow reports its own lifecycle through an activity, and cancellation is written by the
canceller — because a cancelled workflow may not start further activities.

If the projection and the UI ever disagree, Temporal is right about *execution* and `workflow_runs` is right
about *what the product promised* — and the gap between them is a bug worth chasing.

---

## Related

- Design and build order: [docs/impl_temporal.md](impl_temporal.md)
- The bulk-match journey: [docs/impl_bulk_match.md](impl_bulk_match.md)
- Reading logs and traces: [docs/logs_guide.md](logs_guide.md)
- Gateway protections for shared-host routes: [docs/gateway_security.md](gateway_security.md)
