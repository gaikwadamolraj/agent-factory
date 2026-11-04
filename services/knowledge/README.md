# knowledge service

Shared knowledge graph for any internal service: store raw text as graph knowledge (Graphiti on FalkorDB, one graph
per namespace + client + project) and ask grounded, semantic-cached questions. One image, **two processes**:

| Process (k8s Deployment) | Code | Entry point | Does |
|---|---|---|---|
| `ats-knowledge` | `src/ats_knowledge/api/` | `uvicorn ats_knowledge.api.server:app` | MCP server on port 8002: queue text, report status, search, forget. Fast; never extracts |
| `ats-knowledge-worker` | `src/ats_knowledge/worker/` | `python -m ats_knowledge.worker` | Claims queued episodes and extracts them with the LLM (minutes each). No port. Scaled 0..N by KEDA |

They never call each other. They share one table, `knowledge_episodes`: the API inserts rows, the worker claims
them (`FOR UPDATE SKIP LOCKED`).

```
src/ats_knowledge/
├── config.py, metrics.py      settings and metrics (both processes)
├── shared/                    used by BOTH processes
│   ├── episodes.py            ledger table + repository (enqueue, claim, done/failed)
│   ├── tenancy.py             graph id = namespace__c<client>_p<project>
│   ├── graph/                 GraphStore port + Graphiti/FalkorDB adapter
│   ├── graph_guard.py         timeout + circuit breaker for graph calls -> KNW errors
│   ├── graph_versions.py      per-graph change counter (cache invalidation)
│   └── ontology_types.py      caller ontology -> Graphiti types
├── api/                       process ats-knowledge
│   ├── server.py              MCP tools + zero-trust middleware
│   ├── service.py             ingest, get_episode, search, forget, cancel
│   ├── semantic_cache.py      RedisVL answer cache
│   ├── answering.py, prompts.py   grounded answer + citation check
│   └── container.py           wiring
└── worker/                    process ats-knowledge-worker
    ├── __main__.py            entry: telemetry, SIGTERM -> graceful stop
    ├── loop.py                poll -> claim -> run up to WORKER_CONCURRENCY extractions
    ├── extraction.py          one episode: Graphiti extraction -> done / retry / failed
    └── container.py           wiring
```

| MCP tool | Purpose |
|---|---|
| `ingest_episode(request)` | Queue text (+ guidance, optional ontology, `subjectRef`); returns `{episodeId, status, duplicate}`. The worker extracts it section by section |
| `get_episode(clientId, projectId, namespace, episodeId)` | `queued` / `processing` / `done` / `failed` (+ `errorCode`, `sections`) |
| `search_knowledge(request)` | `{answer, citedFactIds, facts, entities, cached, graphVersion}`; `answer=false` skips the LLM; filters `subjectRefs`, `entityTypes`, `relationTypes`, `includeExpired` |
| `update_entity(request)` | Rename an entity / set its attributes (no LLM) |
| `list_episodes(request)` | Ingested episodes by `subjectRef` / `sourceRef`, newest first |
| `forget_source(request)` | Erase everything learned from a `sourceRef` (every section) |
| `cancel_request(requestId)` | Stop a running search |

Contract: `ats_common.knowledge` (models, ontology schema, errors); client: `ats_common.knowledge.client`.
Kubernetes: `deploy/k8s/base/knowledge`, `base/knowledge-worker`, `base/knowledge-stores`, `keda/`.

Docs: [basics](../../docs/knowledge_graph_basics.md) · [ontology guide](../../docs/knowledge_ontology_guide.md) ·
[implementation & security](../../docs/impl_knowledge_graph.md) · [KEDA](../../deploy/k8s/keda/README.md) ·
[roadmap](../../docs/roadmap_candidate_intelligence.md)

Local (env: `.env.example`):

```bash
uv sync
PYTHONPATH=src uv run uvicorn ats_knowledge.api.server:app --port 8002   # API
PYTHONPATH=src uv run python -m ats_knowledge.worker                     # worker
uv run pytest -q
```
