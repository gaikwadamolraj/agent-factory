# KEDA: autoscaling the knowledge worker

[KEDA](https://keda.sh) scales `ats-knowledge-worker` on the length of the knowledge queue (Postgres table
`knowledge_episodes`). No queue in Redis and no extra service are involved: KEDA runs a `COUNT(*)` every 15 s.

```
recruiter uploads resume ─► ats-knowledge (API) ─► INSERT knowledge_episodes (status=queued)
                                                          ▲
KEDA operator ── every 15 s: SELECT COUNT(*) ... ─────────┘
   backlog 0  ─► worker replicas 0   (after 5 min idle)
   backlog ≥1 ─► worker replicas 1..maxReplicaCount
                    └─ each pod: worker/loop.py claims rows with FOR UPDATE SKIP LOCKED (pods never share a row)
```

| File | What it does |
|---|---|
| `kustomization.yml` | Kustomize **Component**; the `dev` and `prod` overlays include it (`components: - ../../keda`) |
| `scaled-object.yml` | The rule: query, target per pod, min 0 / max 1, polling, cooldown |
| `trigger-authentication.yml` | DB login for the query, taken from `ats-platform-secrets` (`postgres-user`, `postgres-password`) |
| `worker-replicas-patch.yml` | Removes the fixed `replicas: 1` from the worker Deployment, so deploys do not fight KEDA |

## Install KEDA (automated)

The operator is managed by the repo: `deploy/k8s/keda-operator` (official release manifest, pinned v2.20.2).
`./deploy.sh` installs it automatically when it is missing; `./deploy.sh keda` installs or upgrades it on demand.
Full guide: [docs/keda_autoscaling.md](../../../docs/keda_autoscaling.md).

## Check it

```bash
kubectl -n ats-platform get scaledobject ats-knowledge-worker      # READY=True, ACTIVE=True while there is backlog
kubectl -n ats-platform get hpa                                    # keda-hpa-ats-knowledge-worker (created by KEDA)
kubectl -n ats-platform describe scaledobject ats-knowledge-worker # events: connection or query errors show here
kubectl -n ats-platform get deploy ats-knowledge-worker            # 0/0 when idle
```

## Tuning

| Setting | Where | Default | Change when |
|---|---|---|---|
| `maxReplicaCount` | scaled-object.yml | 1 | You add LLM capacity (another LM Studio host, a hosted model). More pods on one GPU only queue on that GPU |
| `targetQueryValue` | scaled-object.yml | 1 | You raise `WORKER_CONCURRENCY` (episodes per pod): set them equal |
| `cooldownPeriod` | scaled-object.yml | 300 s | You want the pod kept warm longer between uploads |
| `pollingInterval` | scaled-object.yml | 15 s | Faster wake-up vs. fewer queue queries |

Scale-down safety: a pod being removed gets SIGTERM, stops claiming, finishes its extraction within WORKER_DRAIN_S or
releases it back to the queue at once
(`terminationGracePeriodSeconds: 600`). If it is killed anyway, the episode's lease expires (`WORKER_LEASE_S`)
and another pod retries it.
