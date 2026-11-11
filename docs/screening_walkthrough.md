# A screening, step by step

What actually happens between dropping a resume in the chat box and seeing a score: which service calls
which tool, what gets stored, what comes back, and what is skipped the second time. Verified against the
code on 9 November 2026.

---

## 1. The short version

```
browser → ats-ui → ats-orchestrator ─┬→ agent-guardrail   (refuse injected text)
                                     ├→ postgres          (document + chunks + vectors)
                                     ├→ agent-knowledge-graph  (background, resume only)
                                     └→ agent-skill ──┬→ registry-service  (which version to run)
                                                      ├→ llm-gateway       (4 calls, sequential)
                                                      └→ agent_common.rules (the score — no LLM)
```

Four model calls per screening, run one after another. That is where the time goes, and §2 explains why
they are not parallel.

---

## 2. Why it takes so long, and why the gateway does not split the call

### The four calls are a chain, not a batch

`agent_skill/screening/steps.py` defines four steps and each needs the one before it:

| # | Step | Input it cannot do without | Output |
|---|---|---|---|
| 1 | `analyze_job_description` | the JD text | requirements, importance, min years |
| 2 | `analyze_candidate_profile` | the resume **+ step 1's requirements**, for recall | verified skills, roles, dates |
| 3 | `match_requirements` | steps 1 and 2 | a grade per requirement |
| 4 | `write_verdict` | step 3's grades and score | summary and interview questions |

Step 2 uses step 1's requirement names to catch terms the model missed but the resume spells out. Steps 3
and 4 are hard dependencies. So **three of the four cannot overlap**, and the gateway cannot change that:
LiteLLM is a router. It picks a backend and forwards one request; it does not decompose a prompt or run
sub-calls. There is nothing for it to split.

### What is actually serialising

1. **Model throughput.** With LM Studio on one host there is effectively one model doing one thing at a
   time. The orchestrator caps itself at `LLM_MAX_CONCURRENCY=5` and a worker at
   `TEMPORAL_MAX_CONCURRENT_ACTIVITIES=4`, both bounded deliberately so one burst cannot stampede the
   gateway — but the backend is the real limit.
2. **Prompt size.** Step 3 carries every requirement, every verified skill and every role. Long resumes
   and long JDs cost tokens on the way in as well as out.
3. **The agent loop.** A Strands agent decides which tool to call; its reasoning turns are model calls too.
   Steps it skips are then run directly by a deterministic fallback loop — correct, but it can mean paying
   for the decision *and* the work.

### What would genuinely make it faster

- **Steps 1 and 2 in parallel.** The only pair that could overlap. The cost is step 2 losing the
  JD-term recall pass, so it is a real trade, not free.
- **Warm the JD.** Step 1's result is cached (below), so screening a second candidate against the same JD
  already skips one of the four calls.
- **A smaller model for steps 1 and 2.** They are extraction, not judgement. The gateway already routes by
  purpose alias (`Purpose.EXTRACTION` vs `Purpose.SCREENING`), so this is gateway configuration, not a code
  change.
- **More backend capacity.** Nothing above helps if one model instance is saturated.

---

## 3. What is cached, and what is not

A Redis step cache sits in front of the two extraction calls — `agent_skill/screening/steps.py`:

```python
cached = ctx.cache_get("jd", ws["jd_text"])        # step 1
cached = ctx.cache_get("profile", ws["resume_text"])  # step 2
```

The key is a digest of **the skill version's `cache_id` + the model name + the text**. So:

| Second screening of… | Step 1 (JD) | Step 2 (profile) | Steps 3–4 |
|---|---|---|---|
| the same resume against the same JD | **cached** | **cached** | always re-run |
| a new candidate, same JD | **cached** | runs | runs |
| the same resume, a different JD | runs | **cached** | runs |

Steps 3 and 4 are never cached: they depend on the *pair*, and the pairing is the thing being asked about.
Publishing a new skill version changes `cache_id` and therefore invalidates everything — which is the
behaviour you want when a prompt changes.

---

## 4. The knowledge graph: what gets ingested, and when

### Only the resume. Never the job description.

| Source | Graphed? | Why |
|---|---|---|
| **Resume** | yes — `remember_profile`, in the background on upload | the candidate is the subject the graph is about |
| **Screening outcome** | yes — `remember_screening`, **requirement names and grades only** | so later matching can reason over past screenings |
| **Job description** | **no** | a JD is a *question*, not a subject. It would add a node per vacancy and no facts about anyone |

`knowledge/__init__.py` is explicit about the second row: the screening episode carries the role title,
score, met requirements and gaps — *never the JD text*.

### The same resume does not re-extract

Dedup is in the knowledge service, `api/service.py`:

```python
def content_hash(text, guidance, ontology):
    """Same text + same instructions = same episode (re-sending it is a no-op)."""
```

The hash covers **the text, the extraction guidance, and the ontology's `cache_id`**, and
`knowledge_episodes` is unique on `(graph_id, content_hash)`. Re-sending the same resume returns
`duplicate: true` with the existing episode id — **no extraction, no model call, nothing written**.

Two useful consequences:

- **Changing the pack re-extracts on purpose.** A new ontology or new guidance changes the hash, so the
  same resume is extracted again under the new rules. That is correct: the facts you would get are
  different.
- **A previously failed episode is retried.** If the stored episode is `FAILED`, it is requeued with a
  fresh set of attempts rather than treated as a duplicate.

### And the document itself does not re-upload

Before any of that, `documents.ingest` dedupes on `sha256(text)` per tenant + kind. Re-uploading the same
file returns the existing row (`created=False`) — same `document_id`, so the graph text
(`Source: resume document:42\n\n…`) is identical and the content hash matches. The two layers line up.

> If you want to force a re-extraction, change the pack (guidance or ontology), not the document.

---

## 5. Looking at the graphs in FalkorDB

FalkorDB speaks the Redis protocol with graph commands. One graph per tenant **per project**:

```
graph_id = "<namespace>__c<client>_p<project>"      e.g. talent__c1_p2
```

```bash
kubectl exec -n agent-infra -it falkordb-0 -- redis-cli -a "$FALKORDB_PASSWORD"
```

```redis
GRAPH.LIST                                   # every tenant graph
GRAPH.QUERY talent__c1_p2 "MATCH (n) RETURN labels(n)[0], count(*)"
GRAPH.QUERY talent__c1_p2 "MATCH (n) RETURN n LIMIT 25"
GRAPH.QUERY talent__c1_p2 "MATCH (c:Candidate)-[:HAS_SKILL]->(s:Skill) RETURN c.name, collect(s.name)"
GRAPH.QUERY talent__c1_p2 "MATCH (a)-[r]->(b) RETURN type(r), count(*) ORDER BY count(*) DESC"
```

For a visual browser, port-forward and point FalkorDB Browser at it:

```bash
kubectl port-forward -n agent-infra svc/falkordb 6379:6379
```

And to see what the platform *thinks* it ingested, without touching the graph at all:

```sql
SELECT id, source_type, source_ref, subject_ref, status, attempts, error_code, created_at
  FROM knowledge_episodes ORDER BY id DESC LIMIT 20;
```

`status` tells you whether an episode is `queued`, `processing`, `completed` or `failed` — and the worker
that drains that queue is the one KEDA scales.

---

## 6. The full sequence

What follows is one screening with a resume upload, end to end.

### Upload

| # | Who | Does what | Stores / returns |
|---|---|---|---|
| 1 | browser → ats-ui | `POST /api/backend/v1/chats/{id}/messages`, multipart | — |
| 2 | ats-ui | server-side proxy adds cookies + `X-Client-Id` / `X-Project-Id` | — |
| 3 | ats-orchestrator | middleware: verify JWT via JWKS, check tenant headers against the token and project membership | 401/403 on mismatch |
| 4 | ats-orchestrator | type detected **from the bytes**, size capped | `FILE-INV-0001` / `FILE-LIM-0001` |
| 5 | ats-orchestrator → **agent-guardrail** `check_text` | injection scan **before storage** | refuse with `FILE-INV-0004`; nothing stored |
| 6 | ats-orchestrator | `parse_pdf` (pypdfium2) → text + page count | `FILE-INV-0002` if no text |
| 7 | ats-orchestrator | dedupe on `sha256(text)` per tenant+kind | existing `documents` row, or a new one + blob |
| 8 | ats-orchestrator | `chunk_text` → `embed_documents` via the gateway | `document_chunks` with pgvector embeddings |
| 9 | ats-orchestrator → **knowledge graph** `ingest_episode` | background, resume only | `knowledge_episodes` row, or `duplicate: true` |
| 10 | ats-orchestrator | `start_candidate_onboarding` | a `workflow_runs` row — or nothing, if no engine |

### Screening

| # | Who | Does what | Stores / returns |
|---|---|---|---|
| 11 | ats-orchestrator → **agent-skill** `screen_candidate` | service token, `aud=agent-skill`, tool allow-listed | — |
| 12 | agent-skill → **registry-service** `resolve_skill` | which version this client activated | a bundle, or `None` → packaged module |
| 13 | agent-skill | **step 1** JD analysis → gateway (`Purpose.EXTRACTION`) | requirements; **cached in Redis** |
| 14 | agent-skill | grounding: drop any requirement the JD does not name | `corrections[]` |
| 15 | agent-skill | **step 2** profile → gateway | skills, roles, dates; **cached** |
| 16 | agent-skill | grounding: drop skills not written in the resume; recover JD terms that are | `corrections[]` |
| 17 | agent-skill | `detect()` — a resume that tries to instruct the AI is flagged | `manipulation_detected` |
| 18 | agent-skill | **step 3** match → gateway | a grade per requirement |
| 19 | agent-skill | grounding: a positive grade with no real skill cited is demoted to `absent` | `corrections[]` |
| 20 | agent-skill | years of experience from merged role intervals | `total_years` |
| 21 | agent-skill | **`agent_common.rules.score`** — rubric arithmetic, **no model** | score, band, recommendation, breakdown |
| 22 | agent-skill | **step 4** verdict → gateway | summary, interview questions |
| 23 | ats-orchestrator | store the result, stream it over SSE | `screenings` row + a chat message |
| 24 | ats-orchestrator → knowledge graph | `remember_screening`, background | an episode of **names and grades only** |

### The three places a screening is refused or corrected

- **Step 5** refuses a manipulative document outright, before anything is stored.
- **Steps 14, 16 and 19** silently correct the model: an ungrounded claim is dropped or downgraded, and
  every correction is recorded in `agentTrace.corrections` — visible in the UI.
- **Step 21** is arithmetic. A model can propose a grade; it can never set a score.

---

## 7. Where to look when it is slow

```logql
{service_name="agent-skill"} |= "agent.step"
```

Each step logs its duration and whether the agent chose it or the fallback ran it
(`steps_total{ran_by="agent"|"fallback"}`, `step_duration`). Many `fallback` steps means the agent loop is
burning calls deciding and then not acting — worth tuning the skill's workflow text.

Cache behaviour shows up as `agent.cache_read_failed` / silence; a hit simply means no gateway call for
that step. To confirm a second screening reused the JD analysis, compare step durations between runs.

Related: [docs/logs_guide.md](logs_guide.md), [docs/impl_screening.md](impl_screening.md),
[docs/impl_knowledge_graph.md](impl_knowledge_graph.md), [docs/llm_gateway.md](llm_gateway.md).
