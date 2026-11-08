# KEDA autoscaling (knowledge worker)

How the knowledge worker scales, how KEDA is installed (automated), and how to operate it.
Background on the worker itself: [impl_knowledge_graph.md](impl_knowledge_graph.md).

## 1. In one picture

```
resume upload ─► agent-knowledge-graph (API) ─► INSERT knowledge_episodes (status=queued)         Postgres = the queue
                                                   ▲            ▲
KEDA operator ── every 15 s: SELECT COUNT(*) ──────┘            │ FOR UPDATE SKIP LOCKED
   backlog 0  ─► worker replicas 0 (after 5 min idle)           │
   backlog ≥1 ─► worker replicas 1..max ─► agent-knowledge-graph-worker pod(s) ─► Graphiti ─► FalkorDB
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
| `deploy.sh` (`KEDA_CHART_VERSION`) | The KEDA operator: Helm chart `kedacore/keda` pinned to **2.20.2** (release `keda`, namespace `agent-infra`); `keda` target, installed automatically when missing |
| `deploy/helm/agent-factory/templates/infra/keda/` | The app's scaling rule: `scaled-object.yaml`, `trigger-authentication.yaml` (namespace `agent-factory`) |
| `deploy/helm/agent-factory/values.yaml` (`keda.*`) | Limits, query, Postgres host; `keda.enabled` is on in dev and prod (the worker Deployment then has no `replicas`) |

## 4. Setup (automated)

Prerequisites: `agent-factory-secrets` with `database-url` (KEDA reads the same connection string the services use to
run its count query - no separate database login). The `postgres-clients` NetworkPolicy admits the infra namespace
(`agent-infra`), where the KEDA operator runs.

```bash
./deploy.sh                 # full deploy: installs KEDA if missing, then the release
./deploy.sh keda            # install or upgrade only the KEDA operator
```

What `./deploy.sh keda` runs:

```bash
helm upgrade --install keda keda --repo https://kedacore.github.io/charts --version 2.20.2 \
  -n agent-infra --create-namespace --wait --timeout 10m
```

A KEDA installed earlier from the release manifest is kept as is by `./deploy.sh`; `./deploy.sh keda` asks to
remove it first (`kubectl delete -f https://github.com/kedacore/keda/releases/download/v2.20.2/keda-2.20.2.yaml`).

Connection (`keda.postgres`):

| Field | Value here | Why |
|---|---|---|
| `connectionKey` | `database-url` | KEDA authenticates with the shared `database-url` from `agent-factory-secrets`; its host is the FQDN `postgres.agent-infra.svc.cluster.local`, which the operator resolves from its own namespace |

## 5. Verify

```bash
kubectl -n agent-infra get pods                                               # 3 pods Running
kubectl -n agent-factory get scaledobject agent-knowledge-graph-worker          # READY=True; ACTIVE=True while backlog > 0
kubectl -n agent-factory get hpa keda-hpa-agent-knowledge-graph-worker          # created and owned by KEDA
kubectl -n agent-factory get deploy agent-knowledge-graph-worker                # 0/0 idle, 1/1 with backlog
kubectl -n agent-factory describe scaledobject agent-knowledge-graph-worker     # Events: connection or query errors
```

Live test: upload a resume in the UI, then `kubectl -n agent-factory get pods -w | grep knowledge-graph-worker`; a pod
starts within about 15 s and goes away about 5 minutes after the queue is empty.

## 6. Tuning

| Setting | File | Default | Change when |
|---|---|---|---|
| `maxReplicaCount` | values.yaml `keda.*` | 1 | You add LLM capacity (another LM Studio host, a hosted model). More pods on one GPU/CPU host only queue on it |
| `targetQueryValue` | values.yaml `keda.*` | 1 | You change `WORKER_CONCURRENCY` (episodes per pod); keep them equal |
| `minReplicaCount` | values.yaml `keda.*` | 0 | Set 1 to keep a warm pod (no ~15 s wake-up) |
| `cooldownPeriod` | values.yaml `keda.*` | 300 s | Keep the pod longer between uploads |
| `pollingInterval` | values.yaml `keda.*` | 15 s | Faster wake-up vs. fewer count queries |
| `WORKER_LEASE_S` | values.yaml `services.agentKnowledgeGraph.config` | 4200 s | Must stay above `GRAPH_TIMEOUT_S` |

## 7. Upgrade, disable, remove

* **Upgrade KEDA**: change `KEDA_CHART_VERSION` in `deploy.sh` (read the release notes), then `./deploy.sh keda`.
* **Run without autoscaling**: `keda.enabled: false` in the values file; the worker then runs
  `services.agentKnowledgeGraphWorker.replicas` fixed replicas.
* **Pause scaling** temporarily: `kubectl -n agent-factory annotate scaledobject agent-knowledge-graph-worker
  autoscaling.keda.sh/paused-replicas="1" --overwrite` (remove the annotation to resume).
* **Remove KEDA** from the cluster: delete the ScaledObject first, then
  `helm uninstall keda -n agent-infra`.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| ScaledObject `READY=False` | `describe scaledobject`: wrong `host`/`dbName`, wrong secret keys, Postgres unreachable from namespace `agent-infra` |
| Backlog but 0 pods | `ACTIVE` column; the query only counts due rows (a retry backoff may be pending: `available_at`) |
| Pods start but nothing finishes | Worker logs (`knowledge.episode_failed` with `error`); LM Studio `lms ps` (only Qwen + embeddings should be loaded) |
| `deploy.sh` stops at KEDA rollout | `kubectl -n agent-infra get pods`, `kubectl -n agent-infra logs deploy/keda-operator` |
