# agent-knowledge-graph

Shared knowledge graph for any internal service: store raw text as graph knowledge (Graphiti on FalkorDB, one graph
per namespace + client + project) and ask grounded, semantic-cached questions. One image, **two processes**:

| Process (k8s Deployment) | Code | Entry point | Does |
|---|---|---|---|
| `agent-knowledge-graph` | `src/agent_knowledge_graph/api/` | `uvicorn agent_knowledge_graph.api.server:app` | MCP server on port 8002: queue text, report status, search, forget. Fast; never extracts |
| `agent-knowledge-graph-worker` | `src/agent_knowledge_graph/worker/` | `python -m agent_knowledge_graph.worker` | Claims queued episodes and extracts them with the LLM (minutes each). No port. Scaled 0..N by KEDA |

They never call each other. They share one table, `knowledge_episodes`: the API inserts rows, the worker claims
them (`FOR UPDATE SKIP LOCKED`).

```
src/agent_knowledge_graph/
├── config.py, metrics.py      settings and metrics (both processes)
├── shared/                    used by BOTH processes
│   ├── episodes.py            ledger table + repository (enqueue, claim, done/failed)
│   ├── tenancy.py             graph id = namespace__c<client>_p<project>
│   ├── graph/                 GraphStore port + Graphiti/FalkorDB adapter
│   ├── graph_guard.py         timeout + circuit breaker for graph calls -> KNW errors
│   ├── graph_versions.py      per-graph change counter (cache invalidation)
│   └── ontology_types.py      caller ontology -> Graphiti types
├── api/                       process agent-knowledge-graph
│   ├── server.py              MCP tools + zero-trust middleware
│   ├── service.py             ingest, get_episode, search, forget, cancel
│   ├── semantic_cache.py      RedisVL answer cache
│   ├── answering.py, prompts.py   grounded answer + citation check
│   └── container.py           wiring
└── worker/                    process agent-knowledge-graph-worker
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

Contract: `agent_common.knowledge` (models, ontology schema, errors); client: `agent_common.knowledge.client`.
Kubernetes (Helm chart `deploy/helm/ats-platform`): `templates/services/agents/agent-knowledge-graph`,
`templates/services/agents/agent-knowledge-graph-worker`, `templates/infra/falkordb`, `templates/infra/keda`. Redis: the
platform `redis` (database 0 via `REDIS_DB=0`, needed for the vector-search semantic cache). Callers authenticate
with auth-service service tokens (aud `agent-knowledge-graph`, verified with the JWKS).

Docs: [basics](../../docs/knowledge_graph_basics.md) · [ontology guide](../../docs/knowledge_ontology_guide.md) ·
[implementation & security](../../docs/impl_knowledge_graph.md) · [KEDA](../../docs/keda_autoscaling.md) ·
[roadmap](../../docs/roadmap_candidate_intelligence.md)

Local (env: `.env.example`):

```bash
uv sync
PYTHONPATH=src uv run uvicorn agent_knowledge_graph.api.server:app --port 8002   # API
PYTHONPATH=src uv run python -m agent_knowledge_graph.worker                     # worker
uv run pytest -q
```
