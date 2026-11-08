# Knowledge graph service: implementation, security, operations

Shared platform service: any internal service can store text as graph knowledge and ask questions of it.
Concepts: [knowledge_graph_basics.md](knowledge_graph_basics.md). Ontology and guidance:
[knowledge_ontology_guide.md](knowledge_ontology_guide.md). Future work:
[roadmap_candidate_intelligence.md](roadmap_candidate_intelligence.md).

## 1. Components

```
caller service (orchestrator today)
  │  agent_common.knowledge.client.KnowledgeClient  (service JWT aud=agent-knowledge-graph, timeout, retry, breaker)
  ▼
agent-knowledge-graph  (MCP server, /mcp)                       agent-knowledge-graph-worker  (python -m agent_knowledge_graph.worker)
  ingest_episode ──► knowledge_episodes (Postgres) ◄──── claim (FOR UPDATE SKIP LOCKED)
  get_episode                                            Graphiti.add_episode ──► FalkorDB graph <namespace>__c<client>_p<project>
  search_knowledge ─► semantic cache (Redis 8) ─► Graphiti hybrid search ─► grounded answer (LLM) ─► cache
  forget_source ───► remove graph episodes + ledger rows
  cancel_request
```

Two processes from one image (details and file layout: `services/agents/agent-knowledge-graph/README.md`):

| Process | Code | Role |
|---|---|---|
| `agent-knowledge-graph` | `agent_knowledge_graph/api/` | MCP server: ingest (queue only), status, search, forget, cancel |
| `agent-knowledge-graph-worker` | `agent_knowledge_graph/worker/` | Claims queued episodes, extracts them with Graphiti; scaled 0..N by KEDA |
| shared | `agent_knowledge_graph/shared/` | Ledger/queue, tenancy, graph store, graph guard, graph versions, ontology types |
| contract | `agent_common/knowledge/`, `agent_common/mcp_client.py` | Wire models, ontology schema, `KNW-*` errors, typed client (MCP SDK 1.x and 2.x) |
| ATS use | `ats_orchestrator/knowledge/` | Talent guidance (versioned), when to ingest, the chat tool |

## 2. Flows

**Ingest** (`ingest_episode`, returns in milliseconds)
1. Validate tenant ids, namespace, source ref, sizes, ontology (`KNW-INV-*`, `KNW-LIM-0001`).
2. `content_hash = sha256(text, guidance, ontology id)`. The same hash in the same graph means a duplicate:
   return the existing episode. A previously failed episode is re-queued with fresh attempts.
3. Insert a ledger row with status `queued`; the text is kept for the worker.

**Extract** (worker; pods scaled by KEDA on the backlog, see `docs/keda_autoscaling.md`)
1. Claim up to `WORKER_CONCURRENCY` due rows with `FOR UPDATE SKIP LOCKED` and lease them (`WORKER_LEASE_S`).
2. Split the text into sections (`shared/sections.py`: headings, then packed up to `EPISODE_MAX_CHARS`, 4,000);
   sections after the first start with "(Part of resume document:N. The document begins: ...)" so every section
   resolves the same person. Small prompts are what an 8k model can handle.
3. One `Graphiti.add_episode` per section not done yet, with `custom_extraction_instructions` = security rule +
   ontology rules + caller guidance, and the ontology types (untyped `Entity` nodes are then not created).
   Each section's graph episode id is stored at once (`graph_episode_uuids`), so a retry resumes with the next
   section. Timeout `GRAPH_TIMEOUT_S` per section; own circuit breaker (`knowledge.extraction`).
   Output budget: Graphiti asks for `max_tokens=16384` in edge extraction; `CappedOutputClient` caps every
   request at `LLM_MAX_OUTPUT_TOKENS` (an 8k backend otherwise rejects it, or cuts the JSON off mid-answer).
4. Success: `done`, raw text cleared, graph version bumped. Failure: back to `queued` with exponential backoff
   (60 s, 120 s, ...); after `WORKER_MAX_ATTEMPTS` the status is `failed` and the text is cleared.

**Search** (`search_knowledge`)
1. Scope = graph id + graph version + guidance + ontology + answer mode + filters. Semantic cache lookup (exact
   scope tag, cosine distance ≤ `SEMANTIC_CACHE_DISTANCE`).
2. On a miss: Graphiti hybrid search (BM25 + vector, RRF) in that graph only, with label filters
   (`entityTypes`/`relationTypes`, else the ontology's types). Facts a newer episode contradicted are dropped
   unless `includeExpired`. With `subjectRefs` (one candidate): the ledger gives that subject's graph episode ids,
   the search fetches `limit x SUBJECT_SEARCH_OVERFETCH` results and keeps the facts extracted from those
   episodes, then loads their end nodes exactly (all attributes). Nothing extracted for the subject yet means
   no facts, without a graph query.
3. `answer=true`: one LLM call answers from the facts only and cites ids; code drops citations of facts that
   were not retrieved. No valid citation means "no information".
4. Store in the cache only when facts were found (a graph that is still filling must not cache "nothing").

**Update entity** (`update_entity`): rename / set attributes of one node of the tenant graph (no LLM); graph
version bumped. **List** (`list_episodes`): ledger rows by subject or source, with status and sections.

**Forget** (`forget_source`): remove every graph episode (all sections) of the source (Graphiti also deletes facts that only
that episode supported), delete the ledger rows, and bump the graph version.

**ATS journey**: a resume upload calls `remember_profile` in the background (guidance v2 + the career ontology
`ontologies/talent_v2.json`, `subject_ref=document:N`), so the chat turn does not wait. When screening finishes,
`remember_screening` stores the outcome (role, score, met requirements, gaps; never the JD text) with the same
subject. Every chat turn asks the graph for **this chat's candidate** (`candidate_facts`: subject-scoped, facts
only, 4 s timeout, in parallel with the document search) and adds the lines to the excerpts. The chat tool
`search_candidate_knowledge` answers across all candidates of the project. JD questions use `search_documents`
and are not stored in the graph.

## 3. Tenant isolation (defence in depth)

| Layer | Mechanism |
|---|---|
| Edge | Tenant ids are positive ints, namespace `^[a-z][a-z0-9_]{1,31}$`, source ref allow-listed characters |
| Graph | One FalkorDB graph per `namespace__c<client>_p<project>`; the name is built server-side from validated values and re-checked against a strict pattern |
| Inside the graph | Graphiti stamps `group_id` = graph id on every node and edge; searches pass `group_ids=[graph_id]` |
| Process | Each graph has its own Graphiti instance bound to a driver for that graph only (LRU) |
| Ledger | Every query filters by `graph_id` (which encodes the tenant); unique `(graph_id, content_hash)` |
| Cache | Exact-match scope tag that includes the graph id; entries expire (TTL) |
| Caller trust | The caller (orchestrator) has already checked the user's client + project membership (`TenantMiddleware`); the knowledge service trusts only signed service tokens from allow-listed callers |

## 4. Security

* **Zero trust between services**: short-lived service JWT from the auth-service (client_credentials,
  `aud=agent-knowledge-graph`, `kind=service`, ES256, verified with the JWKS) on every request, caller allow-list (`ALLOWED_CALLERS`), tool allow-list per caller (`TOOL_POLICY`). ClusterIP only;
  a NetworkPolicy lets only the orchestrator reach the MCP port.
* **Stores locked down**: `falkordb` and `redis` (namespace `agent-infra`) require passwords (`agent-infra-secrets`;
  the services read the same values from `agent-factory-secrets`); the FalkorDB browser UI is off; FalkorDB admits
  only agent-factory pods labelled `app.kubernetes.io/component=knowledge-graph`, Redis only agent-factory pods.
* **Prompt injection**: guidance and prompts tell the LLM to treat resume text, facts and questions as data. The
  answer step cannot add facts (citation check). Caller guidance can shape answers but cannot widen their source.
* **PII minimisation**: raw text stays in Postgres only until extraction ends (then cleared); the screening
  outcome sent to the graph contains no JD text; logs never include resume text, guidance, questions or answers
  (ids, counts and codes only); Graphiti's third-party telemetry is disabled (`GRAPHITI_TELEMETRY_ENABLED=false`,
  also forced in code).
* **Fair use**: the talent guidance forbids inferring protected characteristics (age, gender, ethnicity, health).
* **Right to erasure**: `forget_source(source_ref)`. Deleting a client/project cascades its ledger rows; drop
  its FalkorDB graphs with `GRAPH.DELETE <graph_id>` (see Operations).
* **Errors**: catalog codes only (`KNW-*`, `LLM-*`, `SYS-INT-0001`); no stack traces or internal hostnames
  cross the wire (the client hides transport details too).
* **One Redis for the cache**: the platform `redis` is Redis 8 (`redis:8.2-alpine`), which includes the query engine
  (`FT.*`) RedisVL needs; the cache lives in database 0 (`REDIS_DB=0`, the query engine works only there) under the
  `ats:{env}:knowledge:semcache` prefix. Redis runs with `maxmemory-policy volatile-lru`: cache entries carry a TTL
  and may be evicted, the graph-version counters have none and are never evicted. FalkorDB stays separate: a graph
  store must never evict data. The episode queue is the `knowledge_episodes` table in Postgres, not Redis.
* **Supply chain**: images pinned (`falkordb/falkordb:v4.22.3`, `redis:8.2-alpine`); `uv.lock` committed.

## 5. Performance and limits

* Extraction cost: Graphiti makes several LLM calls per episode, more for each extracted entity. Measured on
  Qwen 2.5 14B (LM Studio, `--parallel 5`, `LLM_CONCURRENCY=5`): two sentences about 90 s, a one-page resume
  about 285 s. Hence the background worker, `GRAPH_TIMEOUT_S=3600` and `WORKER_CONCURRENCY=1`
  (one episode already uses all LLM slots).
* Throughput for bulk loads (e.g. 1,000 profiles): about 5 minutes per resume per LLM host, so roughly
  3.5 days on one local GPU. Scale with more LLM capacity (more worker replicas pointed at more LLM endpoints, or
  a hosted model), not with more workers against the same host.
* Search: one embedding call for the cache, graph queries in milliseconds, plus one LLM call on a cache miss
  (`answer=false` skips it). A cache hit costs one embedding call and one Redis query.
* Bounded resources: tenant graph LRU (`GRAPH_CACHE_SIZE`), DB pool, Redis cache memory limit with LRU eviction,
  answer context capped (`MAX_ANSWER_FACTS`), text capped (`MAX_TEXT_CHARS`).

## 6. Configuration

All settings are env vars (see `services/agents/agent-knowledge-graph/src/agent_knowledge_graph/config.py` and `.env.example`). The ones
you will tune:

| Setting | Default | Notes |
|---|---|---|
| Models | aliases `ats-extraction`, `ats-answer`, `ats-embedding` | Real models are chosen in the LLM gateway: [llm_gateway.md](llm_gateway.md) |
| `LLM_CONCURRENCY` | 2 | LLM calls in parallel per extraction. Keep below LM Studio `--parallel` (5) so chat and screening are not starved |
| `GRAPH_TIMEOUT_S` | 3600 | One section's extraction (this host: ~7 tokens/s for Qwen 14B) |
| `EPISODE_MAX_CHARS` | 4000 | Section size per extraction call; smaller = smaller prompts, more calls |
| `SUBJECT_SEARCH_OVERFETCH` | 4 | Subject-scoped search fetches `limit` x this before keeping the subject's facts |
| Orchestrator `KNOWLEDGE_GROUNDING_FACTS` / `_TIMEOUT_S` | 8 / 4 | Facts about this chat's candidate added to each turn; skipped when slower |
| Orchestrator `KNOWLEDGE_GUIDANCE_VERSION` | empty = v2 | v2 = career ontology; v1 = free extraction |
| `WORKER_CONCURRENCY` / `WORKER_LEASE_S` | 1 / 4200 | Lease must exceed the timeout; keep `targetQueryValue` in the ScaledObject equal to `WORKER_CONCURRENCY` |
| KEDA `maxReplicaCount` | 1 | Raise with LLM capacity only |
| `SEMANTIC_CACHE_DISTANCE` | 0.08 | Lower = stricter. Raise carefully: too loose returns answers to different questions |
| `LLM_EMBEDDING_ID` / `LLM_EMBEDDING_DIM` (llm-config) | nomic v1.5 / 768 | Changing the model means new vectors: drop the graphs and the cache index, then re-ingest |
| Orchestrator `KNOWLEDGE_MCP_URL` | k8s: `http://agent-knowledge-graph/mcp` | Empty turns the feature off |
| Orchestrator `KNOWLEDGE_INGEST_SCREENINGS` | true | Store screening outcomes for matching |

## 7. Adding another caller service

1. Depend on `agent-common[mcp]` and use `KnowledgeClient` with your service name as `caller`.
2. Knowledge ConfigMap: add the caller to `ALLOWED_CALLERS` and its tools to `TOOL_POLICY`
   (e.g. `ats-connectors=ingest_episode|get_episode|forget_source`).
3. `deploy/helm/agent-factory/values.yaml (services.agentKnowledgeGraph.networkPolicy.callers)`: add the caller's pod selector.
4. Choose a namespace and guidance (and later an ontology), as in the ontology guide's checklist.

## 8. Operations

```bash
# episode backlog and failures
psql ... -c "select namespace, status, count(*) from knowledge_episodes group by 1,2 order by 1,2;"
# inspect a tenant graph (inside the FalkorDB pod)
redis-cli -a "$FALKORDB_PASSWORD" GRAPH.LIST
redis-cli -a "$FALKORDB_PASSWORD" GRAPH.QUERY talent__c1_p1 "MATCH (n:Entity) RETURN n.name, labels(n) LIMIT 25"
# erase a whole tenant graph (also delete its ledger rows)
redis-cli -a "$FALKORDB_PASSWORD" GRAPH.DELETE talent__c1_p1
```

Metrics (Grafana): `agent_knowledge_ingest_requests_total{outcome}`, `agent_knowledge_episodes_total{outcome}`,
`agent_knowledge_extraction_duration_seconds`, `agent_knowledge_searches_total{cache}`,
`agent_knowledge_search_duration_seconds`, `agent_knowledge_answers_total`. Log events: `knowledge.episode_*`,
`knowledge.cache_unavailable`, `knowledge.worker_*`.

## 9. Known limits

* Free extraction can attach facts loosely (e.g. "the employer uses tool X" from a candidate's bullet). An
  ontology with restricted `source`/`target` reduces this.
* Candidate identity across episodes relies on the shared subject (`document:N`) plus Graphiti's entity
  resolution. A first-class candidate id comes with roadmap phase 2 (`candidates` table).
* Subject-scoped search filters after retrieval (Graphiti has no "facts of these episodes" filter), so for a
  project with very many candidates a subject's facts can fall outside the fetched window; raise
  `SUBJECT_SEARCH_OVERFETCH` or add a graph-side filter when that happens.
* The career ontology's rules are ~1.1k tokens per section prompt; with Graphiti's own prompt this can exceed the
  4B backend's 4k input limit, and the gateway then routes extraction to the bigger backend.
* Free-extraction labels are generic (`Entity`); label filters need an ontology.
* The OTEL SQLAlchemy instrumentation does not support SQLAlchemy 2.1 yet (warning at start-up, platform-wide).
