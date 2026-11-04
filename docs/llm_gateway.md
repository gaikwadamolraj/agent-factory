# LLM gateway and model configuration

Every LLM call of the platform goes through one shared layer (`ats_common.llm`) and one gateway (LiteLLM proxy).
Services never name a real model; they ask for an **alias per purpose**. The gateway decides which real model
serves it, shares load between backends by capacity, and falls back when one fails.

```
agent ─┐                                         ┌─ qwen3-4b      vLLM on 192.168.0.120 (fast, 8k context)
orchestrator ─┼─ ats_common.llm ─► ats-llm-gateway ─┼─ qwen2.5-14b  LM Studio on the host (65k context)
knowledge (+ worker) ─┘  alias per purpose          └─ nomic-embed   LM Studio (embeddings, one model only)
               + client policy        (LiteLLM: least-busy routing, context checks, retries, fallbacks)
```

## 1. Where things are configured

| What | File | Effect of a change |
|---|---|---|
| **Models**: backends, which alias uses which model, routing, fallbacks | `deploy/k8s/base/llm-gateway/litellm-config.yaml` | Gateway rolls automatically (generated ConfigMap `ats-llm-gateway-models`) |
| **Backend addresses** | `deploy/k8s/base/llm-gateway/kustomization.yml` (`ats-llm-backends`) | Gateway rolls automatically |
| **Backend keys + gateway key** | Secret `ats-llm-gateway-secrets` (template `secret.example.yml`) | Restart the gateway |
| **What services ask for**: alias per purpose, per-client policy, timeouts, embedding identity | `deploy/k8s/base/llm-config/kustomization.yml` (`ats-llm-config`) | agent, orchestrator, knowledge and worker roll automatically |
| Code: client, guard, embeddings, policy | `services/common/src/ats_common/llm/` | One place for every service |

Docker Compose mounts the same `litellm-config.yaml`; only the backend addresses differ (environment of
`llm-gateway` in `docker-compose.yml`).

## 2. Aliases (purposes)

| Purpose (`ats_common.llm.Purpose`) | Alias | Used by | Backends today |
|---|---|---|---|
| `CHAT` | `ats-chat` | orchestrator assistant, calculator | qwen3-4b + qwen2.5-14b (least-busy; large prompts go to 14b) |
| `SCREENING` | `ats-screening` | screening agent | qwen2.5-14b; falls back to `ats-chat` |
| `EXTRACTION` | `ats-extraction` | knowledge worker (Graphiti) | qwen3-4b + qwen2.5-14b |
| `ANSWER` | `ats-answer` | knowledge grounded answers | qwen3-4b + qwen2.5-14b |
| `EMBEDDING` | `ats-embedding` | documents, knowledge graph, semantic cache | nomic-embed on LM Studio (never per client, never a different model) |
| profile `high-quality` | `ats-*-quality` | clients with that profile | qwen2.5-14b only; fall back to the normal alias |

## 3. Routing by capacity (LiteLLM router)

* **least-busy**: each request goes to the backend of the alias with the fewest requests in flight.
  `max_parallel_requests` per backend caps it (LM Studio `--parallel 5`, vLLM 4).
* **Pre-call checks**: a request larger than a backend's context window (`model_info.max_input_tokens`) skips that
  backend (e.g. a 9k-token prompt never goes to the 8k qwen3-4b).
* **Retries and cooldown**: a failing backend is retried on another backend of the same alias (`num_retries: 2`)
  and cooled down for 30 s after 3 failures.
* **Fallbacks**: when every backend of an alias fails, the request moves to the fallback alias.
* **Health-check routing** (built into LiteLLM, `general_settings`): every 60 s each backend gets a 1-token probe
  (`BACKGROUND_HEALTH_CHECK_MAX_TOKENS=1`, timeout `model_info.health_check_timeout: 15`). A backend whose probe
  fails (unreachable, connection refused, 5xx) gets no traffic until a later probe passes, so a down or hanging host
  is avoided *before* a user request hits it. A probe that only waited behind busy generation (408) or was rate
  limited (429) is ignored (`health_check_ignore_transient_errors`), so a busy model is not marked down. If every
  backend of an alias is unhealthy, LiteLLM still tries them (no total outage).
  Measured with a hanging backend (unroutable IP): without health routing the first requests failed after ~21 s;
  with it every request succeeded in under 1 s on the healthy backend.
  Check: `curl -H "Authorization: Bearer $KEY" localhost:4000/health` (runs probes now; `/health/latest`
  needs the LiteLLM database, planned with the model admin UI).

Measured on this setup: 8 parallel chat requests split 4 / 4 between the two backends; a 9k-token prompt went to
the 14b; with qwen3-4b down every request still succeeded on the 14b (first one ~10 s with the retry, then ~1 s).

## 4. Per-client model policy

```
LLM_PROFILES        = {"high-quality": {"chat": "ats-chat-quality", "answer": "ats-answer-quality",
                                        "extraction": "ats-extraction-quality"}}
LLM_CLIENT_PROFILES = {"2": "high-quality"}            # client id -> profile (2 = globex)
```

A profile overrides the alias for the purposes it lists; everything else uses the default alias. Unknown profiles
fall back to the defaults. Embeddings are never per client. Every request also carries `user=client-<id>` so the
gateway can attribute usage per client (no personal data).

Typical uses: `high-quality` (big model only), `self-hosted-only` (when a hosted provider is added, a profile whose
aliases contain only on-premise backends, for clients whose contract forbids sending data out).

## 5. Common changes

**Swap or add a model** (e.g. tomorrow `fortune-7b` on `http://10.0.0.5:8000/v1`):
1. `litellm-config.yaml`: add a backend under `x-backends`
   (`model: openai/fortune-7b`, `api_base: os.environ/FORTUNE_API_BASE`, `api_key: os.environ/FORTUNE_API_KEY`,
   `max_parallel_requests`), plus its context under `x-context`, and list it under the aliases that should use it.
2. `llm-gateway/kustomization.yml`: add `FORTUNE_API_BASE=http://10.0.0.5:8000/v1` to `ats-llm-backends`.
3. Secret: add `fortune-api-key`; map it in `llm-gateway/deployment.yml` (`FORTUNE_API_KEY`).
4. `llm-config`: bump `LLM_CONFIG_VERSION` (cached screening results and answers of the old model stop matching).
5. `./deploy.sh orchestrator` (or any service target) applies everything; the gateway rolls by itself.

**Add a hosted provider** (Anthropic, Azure OpenAI, Bedrock, ...): same steps with the provider prefix
(`anthropic/claude-haiku-5-5`); check the data processing agreement before sending resumes out, and keep
self-hosted-only clients on a profile without it.

**Change which model a purpose uses**: edit the alias entries in `litellm-config.yaml` only.

**Never** point `ats-embedding` at a different model without re-embedding: stored vectors (pgvector chunks, graph,
cache) would no longer match. If you must: change `LLM_EMBEDDING_ID`/`LLM_EMBEDDING_DIM`, re-ingest documents,
rebuild the graphs, flush the semantic cache.

## 6. Code (`ats_common.llm`)

| Function | Purpose |
|---|---|
| `model_for(purpose, client_id)` / `model_identity(...)` | Alias for a call / alias + config version for cache keys |
| `gateway_client()` / `gateway_client_args()` | The OpenAI client for the gateway / its arguments for framework clients (Strands, LangChain, Graphiti) |
| `guarded(purpose, call, op)` | Timeout, circuit breaker per purpose, one retry on malformed JSON, catalog errors (`LLM-TMO-0001`, `LLM-UNA-0001`, `LLM-INV-0001`), metric `ats_llm_calls_total{purpose,op,outcome}` |
| `structured_completion(purpose, prompt, Schema, client_id=...)` | JSON-schema completion validated into a Pydantic model |
| `embed_documents` / `embed_query` / `embed_texts` / `embedding_id()` | Embeddings with task prefixes, fixed dimension and identity |

Framework clients stay in the services (`agent/llm.py`, `orchestrator/assistant/graph.py`,
`knowledge/shared/graph/gateway_clients.py`) but are built from `gateway_client_args()` + `model_for(...)`.

## 7. Security

* The gateway is cluster-internal (ClusterIP); a NetworkPolicy allows only agent, orchestrator, knowledge and the
  knowledge worker.
* Prompt and reply logging is off (`turn_off_message_logging`), LiteLLM telemetry is off, the admin UI is off.
* Keys live only in `ats-llm-gateway-secrets`; services hold only the gateway key, never backend or provider keys.
* Non-root container, no privilege escalation, all capabilities dropped.
* Known limit: without a LiteLLM database there are no per-service virtual keys; services share the gateway key
  (mitigated by the NetworkPolicy). Adding a database enables virtual keys, budgets and per-client spend reports.

## 8. Operate

```bash
kubectl -n ats-platform logs deploy/ats-llm-gateway | grep -E "POST /v1|ERROR"
kubectl -n ats-platform port-forward svc/ats-llm-gateway 4000:80
curl -s -H "Authorization: Bearer $KEY" localhost:4000/v1/models          # aliases
curl -s -H "Authorization: Bearer $KEY" localhost:4000/health             # backend health (calls each backend)
```

The response header `x-litellm-model-api-base` shows which backend served a request. Grafana: `ats_llm_calls_total`
and `ats_llm_call_duration_seconds` by purpose.
