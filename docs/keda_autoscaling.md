# KEDA autoscaling (knowledge worker)

How the knowledge worker scales, how KEDA is installed (automated), and how to operate it.
Background on the worker itself: [impl_knowledge_graph.md](impl_knowledge_graph.md).

## 1. In one picture

```
resume upload ─► ats-knowledge (API) ─► INSERT knowledge_episodes (status=queued)         Postgres = the queue
                                                   ▲            ▲
KEDA operator ── every 15 s: SELECT COUNT(*) ──────┘            │ FOR UPDATE SKIP LOCKED
   backlog 0  ─► worker replicas 0 (after 5 min idle)           │
   backlog ≥1 ─► worker replicas 1..max ─► ats-knowledge-worker pod(s) ─► Graphiti ─► FalkorDB
```

* **Backlog** = episodes being extracted (`processing`) + episodes due now (`queued`, `available_at <= now()`).
  Queued rows still waiting out a retry backoff do not wake a pod.
* **One pod per backlog item** (`targetQueryValue: 1` = `WORKER_CONCURRENCY: 1`), capped by `maxReplicaCount`.
* Many pods are safe: `SKIP LOCKED` means a row claimed by one pod is invisible to the others.

## 2. What happens when a worker dies

| Event | Result |
|---|---|
| Pod crashes or is evicted mid-extraction | Row stays `processing` with a lease (`WORKER_LEASE_S`, 70 min). KEDA still counts it, so a pod is kept or started. When the lease expires the next claim picks it up again (attempt + 1) |
| Extraction fails (LLM error, timeout) | Row goes back to `queued` with backoff 60 s, 120 s; after `WORKER_MAX_ATTEMPTS` (3) it becomes `failed` (`errorCode` explains why) |
| KEDA scales a pod down / deploy | The pod gets SIGTERM, stops claiming, finishes its extraction if it can within `WORKER_DRAIN_S` (540 s), otherwise hands it straight back to the queue (`status=queued`, attempt not counted). No waiting for the lease |
| FalkorDB not ready at start | The worker waits with backoff (`knowledge.worker_waiting_for_stores`) instead of crashing |
| KEDA itself is down | Existing replicas keep running and processing; only scaling pauses |

Nothing is lost: the job lives in Postgres until it is `done` or `failed`.

## 3. Files

| Path | Purpose |
|---|---|
| `deploy/k8s/keda-operator/kustomization.yml` | The KEDA operator: official release manifest pinned to **v2.20.2** (cluster-wide, namespace `keda`) |
| `deploy/k8s/keda/` | Kustomize **Component** with the app's scaling rule: `scaled-object.yml`, `trigger-authentication.yml`, `worker-replicas-patch.yml` |
| `deploy/k8s/overlays/dev`, `prod` | Include the component: `components: - ../../keda` |
| `deploy.sh` | `keda` target; installs the operator automatically when it is missing |

## 4. Setup (automated)

Prerequisites: the secret `ats-platform-secrets` with `postgres-user` and `postgres-password` (already there; KEDA
uses the same login as the services to run its count query).

```bash
./deploy.sh                 # full deploy: installs KEDA if missing, then schema + all services
./deploy.sh keda            # install or upgrade only the KEDA operator
./deploy.sh knowledge       # also installs KEDA first if the cluster does not have it
```

What `./deploy.sh keda` runs:

```bash
kubectl apply --server-side --force-conflicts -k deploy/k8s/keda-operator
kubectl wait --for=condition=established crd/scaledobjects.keda.sh crd/triggerauthentications.keda.sh
kubectl -n keda rollout status deployment/keda-operator          # + keda-metrics-apiserver, keda-admission
```

The app overlay then applies the `ScaledObject` and `TriggerAuthentication` in `ats-platform`.

Two values in `deploy/k8s/keda/scaled-object.yml` depend on the cluster. Check them for a new cluster:

| Field | Value here | Why |
|---|---|---|
| `host` | `postgres-service.amol.svc.cluster.local` | Full DNS name: the KEDA operator runs in namespace `keda`, where the short alias does not resolve |
| `dbName` | `ats_platform` | Same database as `DATABASE_NAME` in the ConfigMaps |

## 5. Verify

```bash
kubectl -n keda get pods                                               # 3 pods Running
kubectl -n ats-platform get scaledobject ats-knowledge-worker          # READY=True; ACTIVE=True while backlog > 0
kubectl -n ats-platform get hpa keda-hpa-ats-knowledge-worker          # created and owned by KEDA
kubectl -n ats-platform get deploy ats-knowledge-worker                # 0/0 idle, 1/1 with backlog
kubectl -n ats-platform describe scaledobject ats-knowledge-worker     # Events: connection or query errors
```

Live test: upload a resume in the UI, then `kubectl -n ats-platform get pods -w | grep knowledge-worker`; a pod
starts within about 15 s and goes away about 5 minutes after the queue is empty.

## 6. Tuning

| Setting | File | Default | Change when |
|---|---|---|---|
| `maxReplicaCount` | scaled-object.yml | 1 | You add LLM capacity (another LM Studio host, a hosted model). More pods on one GPU/CPU host only queue on it |
| `targetQueryValue` | scaled-object.yml | 1 | You change `WORKER_CONCURRENCY` (episodes per pod); keep them equal |
| `minReplicaCount` | scaled-object.yml | 0 | Set 1 to keep a warm pod (no ~15 s wake-up) |
| `cooldownPeriod` | scaled-object.yml | 300 s | Keep the pod longer between uploads |
| `pollingInterval` | scaled-object.yml | 15 s | Faster wake-up vs. fewer count queries |
| `WORKER_LEASE_S` | base/knowledge/config.yml | 4200 s | Must stay above `GRAPH_TIMEOUT_S` |

## 7. Upgrade, disable, remove

* **Upgrade KEDA**: change the version in `deploy/k8s/keda-operator/kustomization.yml` (read the release notes),
  then `./deploy.sh keda`.
* **Run without autoscaling**: remove the `components:` lines from the overlay; the worker then runs as one fixed
  replica (from `base/knowledge-worker/deployment.yml`).
* **Pause scaling** temporarily: `kubectl -n ats-platform annotate scaledobject ats-knowledge-worker
  autoscaling.keda.sh/paused-replicas="1" --overwrite` (remove the annotation to resume).
* **Remove KEDA** from the cluster: delete the ScaledObject first, then
  `kubectl delete -k deploy/k8s/keda-operator`.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| ScaledObject `READY=False` | `describe scaledobject`: wrong `host`/`dbName`, wrong secret keys, Postgres unreachable from namespace `keda` |
| Backlog but 0 pods | `ACTIVE` column; the query only counts due rows (a retry backoff may be pending: `available_at`) |
| Pods start but nothing finishes | Worker logs (`knowledge.episode_failed` with `error`); LM Studio `lms ps` (only Qwen + embeddings should be loaded) |
| `deploy.sh` stops at KEDA rollout | `kubectl -n keda get pods`, `kubectl -n keda logs deploy/keda-operator` |
