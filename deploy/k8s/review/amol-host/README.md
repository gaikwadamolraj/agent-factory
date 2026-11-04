# Review: serve ats-platform on amol.rackspire.ai (NOT applied by deploy.sh)

The existing ingresses in namespace `amol` stay exactly as they are:

| Existing (amol) | Priority | Path | Backend |
|---|---|---|---|
| frontend-v1-ingress | 110 | `/v1`, `/_next`, `/api/evaluate` (Exact) | ats-frontend-service (amol) |
| api-ingress | 42 | `/api` | api-service (amol) |
| website-root-ingress | 10 | `/` | ats-frontend-service (amol) |

The new frontend is built with `basePath=/ats`, so all its pages, assets (`/ats/_next/...`) and its API proxy
(`/ats/api/backend/...`) live under `/ats` and never collide with `/_next`, `/api` or `/v1` above.

| New (ats-platform) | Priority | Path | Backend |
|---|---|---|---|
| ats-amol-app | 120 | `/ats` | ats-frontend (ats-platform) |
| ats-amol-grafana | 120 | `/grafana` | ats-lgtm (ats-platform) |

Apply after review:

```bash
kubectl apply -f deploy/k8s/review/amol-host/ingress-app.yml
```
Roll back: `kubectl -n ats-platform delete ingress ats-amol-app ats-amol-grafana`.
