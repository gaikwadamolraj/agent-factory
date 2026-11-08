# Domain-neutral agent platform — target architecture and delta plan

**Status:** proposed, not built. Re-frames docs/platform.md, docs/impl_agent_platform.md,
docs/impl_temporal.md and docs/knowledge_ontology_guide.md around one common platform with domain packs.
**Date:** 7 November 2026
**Inputs:** `domain_aware_mcp_agent_mesh_architecture.md` (v2.0), the common-agent-platform diagram,
docs/existing_comment.md (items 4, 5, 17, 22, 23, 24, 25, 26, 28), the ontology work on `feat/ontology`,
the 2026-11-06 build checkpoint, and the repository as it is today.

**Repo:** renamed to **agent-factory** (`git@github.com:gaikwadamolraj/agent-factory.git`). The local folder is
still `ats-platform`; rename it when no session is running. The Helm chart and namespace are already
`agent-factory`.

**What changed since this plan was first written (7 November 2026):** the repo rename above; `agent-factory`
(the service), `admin-ui` and `agent-document` are now built and on disk, and `agent-document` is wired into
the chart and compose; the three 2026-11-06 checkpoint remainders are closed (§7); and `agent-factory` turned
out to already carry a `ToolCatalogue` — a registry of MCP servers with policy-gated proxy invocation — which
moves the capability catalogue (§4.3) from missing to partial. §4 is the layer-by-layer reading of the v2.0
architecture against the tree as it stands after all of that.

---

## 1. The target in one sentence

One **common agent platform** — planner, durable workflow, skills registry, ontology registry, memory,
execution engine — with **shared infrastructure** underneath (documents, ontology/KG, vector search, sandbox,
MCP gateway, rules, audit, storage), and **recruitment, insurance and banking as domain packs on top**.
Recruitment (ATS) is the first pack, not the platform.

Adding a domain must mean: register a pack (skills, MCP tool contracts, **ontology + guidance**, policies,
rules, execution profile) and deploy its MCP service. It must not mean editing the orchestration core.

---

## 2. Honest state of the repo against that target

Most of the **shared infrastructure** row of the target diagram already exists and is sound. The divergence is
concentrated in the **orchestration plane** and in **naming and assets that bake "ATS" into the core**.

### 2.1 Already matches the target — keep, do not churn

| Target element | In the repo today |
|---|---|
| LLM access behind a gateway | LiteLLM `llm-gateway` + `agent_common.llm` with `Purpose` aliases — no model names in services |
| Ontology / KG | `agent-knowledge-graph` (Graphiti on FalkorDB, graph per tenant) + extraction worker + semantic cache |
| Ontology model | `agent_common.knowledge.ontology` — typed entities/relations/attributes, extraction instructions, `parse_ontology` authoring format, validated on every call; the service hard-codes **no** domain (see §3) |
| Document services | `agent-document` (PDF/DOCX/XLSX extractors, chunking, detection, guardrails) |
| Sandbox runtime | `agent-sandbox` (no network, no secrets, time/memory limits) + `agent-compute` |
| Guardrails / prompt safety | `agent-guardrail` + `agent_common.prompt_safety`, untrusted text neutralised/fenced |
| Skills registry + authoring plane | `agent-factory` service (skills, versions, lifecycle, approvals, IAM policy, MCP-to-API wrapper) + `admin-ui` |
| Zero-trust identity | `auth-service` ES256 + JWKS, local verification everywhere, `authz_ver` snapshot cache, service tokens with `aud` + `ALLOWED_CALLERS` + tool allow-list |
| Multi-tenancy | `clients -> projects`, `TenantScope`/`TenantRepository`, `client_id`/`project_id` on every tenant table, RLS on the platform tables |
| Autoscaling | KEDA (on the extraction worker today) |
| Deployment | one Helm chart + `deploy.sh`, generic `backend.yaml`/`config.yaml` ranging over values blocks, expose-nothing-by-default |
| Secrets | k8s Secrets + pluggable `agent_common.secrets` (OpenBao/cloud later) |
| Observability | OTel to LGTM, business metrics, request/trace correlation |
| Cache | one Redis 8, `redis_keys.build_key` registry, volatile-lru, JWKS + authz caches |

That list is the bulk of the platform and it stays exactly as it is.

### 2.2 Genuine divergences — these are the things to fix

**D1. The core is named and shaped for ATS.**
`agent_common`, `services/ats/`, `ats-orchestrator` as *the* orchestrator, `agent-skill/skills/ats_screening`,
metrics `ats_*`. existing_comment §5 and §17 already say this: `agent_common`, and "agent-factory is our main,
ats is one of the clients". Insurance or banking today would either fork the orchestrator or hang off an
ATS-shaped core.

**D2. No planner / executor split.**
The LangGraph assistant in `ats-orchestrator/assistant/` both reasons *and* drives execution, with
`step_budget`, `turn_registry` and a custom LLM queue standing in for a durable workflow. The target is:
LangGraph **proposes a typed DAG**, a policy gate **validates** it, Temporal **owns execution**. Temporal is
designed (docs/impl_temporal.md) but there is no Temporal code in the tree — no `agent_common.workflow`, no
workers, no task queues.

**D3. No domain pack contract.**
The `agent-factory` service has skills, versions, approvals and IAM policy — most of a registry — but there is
no declarative, versioned, signed **pack** binding skills + MCP services + allowed tools + **ontology version +
guidance version** + rules + execution profile + data policy + approval requirements, and no plan validator
that checks a proposed plan against it before execution.

**D4. Shared capability code and domain assets live inside the ATS product service.**
Blob store and chunking sit in `ats-orchestrator/services/documents/`; the vector/embedding path is split
between the orchestrator and `agent-document`; scoring and grounding sit in
`agent-skill/screening/{scoring,grounding}.py` with the job hard-coded (existing_comment §28); and the
**ontologies and guidance live in `ats-orchestrator/knowledge/`** (`ontologies/talent_v2.json`,
`guidance.py`). The diagram's "Storage & Retrieval", "Vector Search", "Scoring & Rules Engine" and
"Ontology / KG" are shared boxes fed by **pack assets**, not product-service source files.

**D5. No tenant priority, fairness or quotas; KEDA is doing one job only.**
One KEDA ScaledObject (extraction worker). No priority tier derived from the tenant plan, no weighted fair
scheduling, no per-tenant concurrency limit, no dead-letter path. KEDA cannot supply this — it scales replicas,
it does not decide whose work runs first.

**D6. The edge is not MCP-aware.**
Traefik routes HTTP. Tool-level policy, per-caller limits and tool allow-lists are enforced in-process by
`ToolPolicyMiddleware` — correct as defence in depth, but there is no L7 MCP gateway in front. `agentgateway`
is the candidate; Google Agent Mesh is explicitly a later pilot (pre-1.0, not an officially supported Google
product).

**D7. Sandbox isolation is container-level only.**
No `runtimeClass` (gVisor/Kata) anywhere in the chart. gVisor or the NVIDIA sandbox runtime is a
`runtimeClassName` on the sandbox pod plus node pool setup, not a rewrite.

**D8. Name collision.** `agent-factory` is now the repo, the chart and the namespace **and** a service
(`services/platform/agent-factory`). The platform keeps the name; the service is renamed (§8.1).

---

## 3. Ontology — already right at the service, wrong at the ownership boundary

This is the part that was designed most carefully, and it should not be rebuilt. Stating what it already gives
the multi-domain target:

- **The knowledge service hard-codes no domain.** Every caller brings its own `guidance` and optional
  `ontology` per call, validated against `agent_common.knowledge.ontology` before anything is stored
  (`KNW-INV-0002`). That is exactly the domain-neutral shared capability the target asks for — insurance and
  banking need no change to `agent-knowledge-graph`.
- **The ontology model is richer than the pack schema in the v2.0 doc**, which carries only
  `ontology: {schema, version}`. Ours has typed entities, relations with source/target restriction, typed and
  enum attributes, reserved-name protection, up to 40 extraction instructions, and the `edges`/`edge_map`
  authoring format with `parse_ontology`. The pack should *reference a registered ontology version*, not
  re-describe it.
- **Versioning discipline is already the target's "version everything".** Released versions are never edited;
  the ontology is part of the episode content hash and of the cache scope, so `v2` re-ingests cleanly and never
  serves `v1` answers.
- **`namespace` is already part of every graph id**, so several domains share the KG service safely; and
  `subject_ref` / `subject_refs` give the per-record view (candidate, claim, customer) without splitting the
  graph per record — which is what keeps cross-record matching possible.
- **The onboarding recipe generalises to every new pack** (guide §6): pick a namespace, write guidance, start
  with free extraction, review the real graph, freeze `v1`, register the caller. That is the per-domain
  ontology procedure for insurance, banking and anything after.

What actually has to move:

1. **Ontology and guidance become pack assets, not product-service files.** `talent_v2.json` and
   `guidance.py` leave `ats-orchestrator` and are registered as versioned artefacts in the registry service,
   pinned per run alongside the skill and tool versions. A pack declares
   `ontology: {name, version}` + `guidance: {ingest_version, answer_version}`; the planner reads them, the
   validator checks them, the run records which versions it used.
2. **One ontology registry, same lifecycle as skills** (draft -> in_review -> published -> archived, approval,
   diff in admin-ui). Ontology changes are the highest-blast-radius change in the system — a new version
   re-ingests a whole namespace — so they get the approval flow that already exists for skills, not a file
   edit.
3. **Cache scope includes the pinned ontology version** everywhere, including the semantic cache
   (existing_comment §22: never hit the vector store when a versioned cached answer exists; a version bump
   refreshes).
4. **Per-domain namespaces and ontologies** become the normal case: `talent` (ATS), `claims`/`policy`
   (insurance), `kyc`/`counterparty` (banking) — each its own namespace, ontology version and guidance,
   all on the one KG service.
5. **Provenance.** Every finding carries the ontology version, episode ids and subject refs it came from, so a
   report is reproducible from pinned versions (the v2.0 doc's acceptance criterion).

---

## 4. The v2.0 architecture, layer by layer, against this repo

Checked against the working tree on 7 November 2026. **Built** = exists and is wired into the chart.
**Partial** = exists but not at the boundary or scope the v2.0 design asks for. **Missing** = not in the tree.

### 4.1 Responsibility split (v2.0 §1)

| v2.0 layer | Status | Where it is today | Stage |
|---|---|---|---|
| Planning and reasoning (LangGraph) | Partial | `ats-orchestrator/assistant/` — a LangGraph assistant that reasons **and** drives execution. It does not emit a typed DAG for a validator to approve. | D |
| Durable workflow (Temporal) | Missing | designed in docs/impl_temporal.md; no code, no workers, no task queues | C |
| Domain capabilities (MCP services) | Partial | every agent is already a real MCP service with a bounded tool set; but ATS domain logic is split between `ats-orchestrator` and `agent-skill/skills/ats_screening`, so there is no `ats-mcp` boundary to swap per domain | D |
| Agent-to-service access (agentgateway) | Missing | Traefik routes HTTP; tool policy is enforced in-process by `ToolPolicyMiddleware` + `ALLOWED_CALLERS` (keep this either way — defence in depth) | G |
| Private agent network (Agent Mesh) | Not started, deliberately | pre-1.0 and not an officially supported Google product; pilot only when cross-cluster / on-prem is a real requirement | G |
| Cluster networking | Built | Services + cluster DNS + per-backend NetworkPolicies, expose-nothing-by-default (only `ats-ui` has a route) | — |
| Autoscaling (KEDA) | Partial | one ScaledObject, on the knowledge extraction worker. No per-pool scaling, no backlog-age signal. | F |
| Data services | Built | Postgres (pgvector) + Redis 8 + FalkorDB + blob store; object storage is a blob store **inside** `ats-orchestrator` rather than a shared service | E |
| Code execution sandbox | Built, minus isolation | `agent-sandbox` (no network, no secrets, limits) + `agent-compute`; no gVisor/Kata `runtimeClass` | G |

### 4.2 Shared platform services (v2.0 §11)

| v2.0 service | Status | Where it is today | Stage |
|---|---|---|---|
| Document MCP | **Built** | `agent-document` — `read_document`, `scan_document`, `chunk_document`; PDF/DOCX/XLSX extractors, guardrail-scanned. Wired into the chart and compose on 7 Nov. No OCR yet. | — |
| KG MCP | **Built** | `agent-knowledge-graph` (Graphiti on FalkorDB), caller-supplied validated ontology, namespaces, subjects, semantic cache | — |
| Vector MCP | Partial | no service: hybrid RRF search (pgvector + full-text) lives in `ats-orchestrator/repositories`, embeddings in `agent_common.llm.embeddings`, chunking in `agent-document` | E |
| Sandbox MCP | Built | `agent-sandbox` behind `agent-compute` | G (isolation) |
| Rules MCP | **Missing** | `agent-skill/screening/{scoring,grounding}.py` — deterministic scoring exists but is hard-coded inside the screening skill, not a shared versioned capability | E |
| Model Gateway | **Built** | LiteLLM `llm-gateway` + `agent_common.llm` `Purpose` aliases; no model name or provider key in any service | — |
| Notification MCP | Partial | `agent_common.notifications` (model + `notify()` + pluggable notifier); in-app/copyable link only, no SMTP/Slack/Teams | later |

### 4.3 Registry, packs and the plan gate (v2.0 §6, §7, §10)

| v2.0 element | Status | Where it is today | Stage |
|---|---|---|---|
| Skill registry with versions and approvals | **Built** | `agent-factory`: draft → in_review → published → archived, approval flow, notifications, IAM policy, delegated admin, `admin-ui` | — |
| MCP capability catalogue | Partial | `agent-factory` `ToolCatalogue` — a refreshable registry of MCP servers (name + base URL + audience) with a client factory and policy-gated proxy invocation (`tool:{server}.{tool}`, default deny). What is missing is the **stored, versioned snapshot of each tool's schema** that v2.0 §10 asks for; the catalogue reaches live servers. | B |
| Domain pack contract | Missing | nothing binds skills + tools + ontology + rules + execution profile + data policy into one pinned, signed artefact | B |
| Plan validator / policy gate | Missing | no DAG to validate yet; tool authorisation exists per call, not per plan | B |
| Versioned tool schemas + provenance | Partial | tool inputs are validated (`extra="forbid"`); results are not pinned to a tool version in a run record | B/C |

### 4.4 Cross-cutting (v2.0 §8, §12, §13, §14, §16)

| v2.0 concern | Status | Where it is today | Stage |
|---|---|---|---|
| Priority, fairness, quotas | **Missing** | no tier derived from the tenant plan, no weighted fair dispatch, no per-tenant concurrency cap, no dead-letter path | F |
| Request identity envelope | Partial | JWT (user `aud=ats-api` / service `aud=<service>`) + `X-Client-Id`/`X-Project-Id` + membership + `authz_ver` + request/trace ids. Missing from the envelope: `domain_id`, `run_id`/`workflow_id`, approved priority tier, data classification, residency. | B, F |
| Default deny, tenant isolation | **Built** | local JWKS verification in every service, `ALLOWED_CALLERS` + tool allow-list, `TenantScope`/`TenantRepository`, `client_id`/`project_id` on every tenant table, RLS on the platform tables | — |
| Secrets | Built | k8s Secrets + pluggable `agent_common.secrets`; never rendered by the chart | — |
| State and persistence | Partial | Postgres / Redis / KG / blob are in place. Missing: Temporal history, plan snapshots, `workflow_runs`, `artifacts`/`artifact_subjects`. | C |
| Observability | Built (base) | OTel → LGTM, business metrics, correlated request/trace ids. Missing: queue depth and oldest-job age, KEDA scale events, per-tier wait vs execution time. | F |
| Failure handling | Partial | `resilient_call` with timeout + bounded retry + named circuit breaker on every outbound call; cancellation propagates to the LLM stream. Missing: workflow-level resume and idempotency keys on non-read tools. | C |
| Human approval for consequential decisions | Partial | approval flow exists for **skills**; not for run outcomes | B |

### 4.5 Summary

Of the v2.0 design, the **shared-infrastructure and security layers are largely built** — document, KG, sandbox,
model gateway, skill registry, zero-trust identity, tenant isolation, deployment and observability. What is
missing is almost entirely the **orchestration plane**: Temporal, the planner/validator split, the domain pack,
the rules capability as a shared service, priority scheduling, and the MCP-aware gateway. That is what stages
B–G below build, in that order.

---

## 5. Target shape

```
                     +--------------- Common agent platform ----------------+
   client --> gateway| intake --> planner (LangGraph) --> plan validator     |
   (agentgateway)    |              |                        |               |
                     |              |                   Temporal (durable)   |
                     |      pack / skill / ontology           |              |
                     |      registry (registry-service) priority scheduler   |
                     +----------------------------------------+-------------+
                                                    per-domain / per-tier queues
                                                               |  (KEDA scales each pool)
            +--------------+---------------+-------------------+---------+
        ats-mcp      insurance-mcp     banking-mcp                  (future packs)
            +--------------+---------------+-----------------------------+
                                   | MCP
   shared: agent-document . agent-knowledge-graph . vector . agent-sandbox/compute .
           rules . agent-guardrail . blob store . auth-service . llm-gateway . lgtm
```

Ownership rule, stated once: **LangGraph plans, Temporal executes, MCP services do the work.** They never
compete for the same state. Large payloads (PDFs, graph results) go to object storage; only references, hashes
and small metadata cross the workflow boundary.

---

## 6. Delta plan

Nothing in §2.1 is touched except for mechanical renames. Deploy stays on hold throughout.

### Stage A — de-ATS the core (mechanical, no behaviour change)

- `ats_common` -> `agent_common` (existing_comment §5). Package, imports, `services/common` metadata. **DONE 7 Nov.**
- Metrics prefix `ats_*` -> platform-neutral (`agent_llm_calls_total`, `agent_llm_call_duration_seconds`,
  `agent_turns_total`, ...); domain stays a low-cardinality label, not part of the name. **DONE 7 Nov** - 34
  instruments renamed, `ats_factory_*` -> `agent_registry_*`, Grafana dashboard queries updated.
- Rename the `agent-factory` **service** (§8.1) so the platform owns the name. **DONE 7 Nov** — it is now
  `registry-service` (package `registry_service`, audience/DNS `registry-service`, Helm key `registryService`,
  secret `client-secret-registry-service`, `JWT_AUDIENCE_REGISTRY`, admin-ui `INTERNAL_REGISTRY_URL`). The
  public admin-console path stays `/agent-factory` — it is the platform console, and the platform keeps that
  name.
- Error-code `DOMAIN` set gains `PLN` (planning) and `SCH` (scheduling); `SCR` (screening) becomes pack-local.
  **DONE 7 Nov** (agent_common/errors.py + CLAUDE.md §5).

### Stage B — pack contract: skills + tools + ontology + rules, registered and validated

- Pack schema (v2.0 doc §6, extended with the ontology/guidance references from §3): skills, mcpServices +
  allowedTools, ontology version, guidance versions, rules, execution profile, data policy, human-approval
  list. Versioned, schema-validated and signed on activation, stored beside the existing
  skills/versions/approvals tables (external UUID + audit + soft delete + RLS, existing_comment §23).
- Ontology + guidance registry with the skill lifecycle and approval flow; admin-ui gets an ontology tab with
  version diff.
- `agent_common.pack`: load and pin a pack version (and with it the ontology version) for a run.
- Plan validator: every tool in a proposed DAG must exist in the pinned pack and be permitted for that tenant;
  default deny; an invalid plan goes back to the planner with bounded feedback, never straight to execution.

### Stage C — Temporal as the execution owner

- `agent_common.workflow` (client + worker runner, settings-driven, business logic framework-free) per
  docs/impl_temporal.md §2.
- Temporal server + UI in `templates/infra/temporal/`, persistence on the existing Postgres.
- Move `run_skill` and the candidate-onboarding pipeline onto workflows; retire `step_budget`, `turn_registry`
  and the custom LLM queue as workflow concerns.
- `workflow_runs` + `artifacts` / `artifact_subjects` so step outputs are reused to answer later questions
  instead of re-calling the LLM; Temporal history is the step record (no `workflow_steps` table).
- Cancellation stays end to end: Temporal cancel -> activity cancel -> MCP cancel -> LLM stream close.

### Stage D — split the planner out of the ATS orchestrator

- Planner becomes a domain-neutral LangGraph activity: classify request -> read pack metadata -> emit a typed
  DAG with inputs, outputs, timeouts, retries, tenant, priority, permissions.
- `ats-orchestrator` keeps only the ATS product API and ATS domain logic; its screening skill, scoring,
  grounding, ontologies and guidance move into the ATS pack and an `ats-mcp` tool surface.
- Guardrail scope (existing_comment §4): off-topic requests ("write me some code") are refused by the planner
  against the pack's capability list, not only by prompt text.
- Chat state and correlation (existing_comment §6, §26): orchestrator owns conversation state, a chat id per
  agent-to-agent conversation, and one correlation id from the user request through every downstream call.

### Stage E — shared capabilities out of the product service

- Blob store + chunking out of `ats-orchestrator` into shared platform code/service.
- One vector surface (embed / upsert / retrieve / rerank) behind an interface, pgvector today.
- `rules` capability: deterministic scoring, thresholds, decision tables, versioned — never LLM arithmetic.
  This is where `screening/scoring.py` and `grounding.py` land, as pack-supplied rule/skill files executed with
  runtime JSON data (existing_comment §28), not hard-coded Python.

### Stage F — priority, fairness and scaling

- Request envelope: `run_id`, `tenant_id`, `domain`, `capability`, `priority_tier`, `deadline`,
  `idempotency_key`, `data_classification`, `workflow_id`, `trace_id`. **`priority_tier` is derived
  server-side from the tenant plan — never trusted from the client.**
- Per-domain task queues x tiers (premium / standard / batch), weighted fair dispatch, per-tenant concurrency
  caps, dead-letter and retry queues.
- One KEDA ScaledObject per worker pool, scaling on backlog and oldest-message age — by domain/tier pool, not
  per tenant; tenant limits are enforced in the scheduler.

### Stage G — edge and isolation

- Evaluate `agentgateway` as the MCP/A2A-aware L7 gateway in front; keep in-process tool policy as defence in
  depth either way.
- `runtimeClassName: gvisor` (or the NVIDIA sandbox runtime) on `agent-sandbox`, plus node pool setup; keep
  default-deny egress and no long-lived secrets in the sandbox.
- Google Agent Mesh: pilot only when cross-cluster / on-prem connectivity is a real requirement. Pre-1.0 and
  not an officially supported Google product — not a production dependency.

### Stage H — second domain, as proof

Register an insurance (or banking) pack — its own namespace, ontology version, guidance, tools and rules — and
run it **with no change to the orchestration core**. That is the acceptance test for the whole exercise; if
core code needs a domain branch, the split is not done.

---

## 7. Reconciling the in-flight queue (2026-11-06 checkpoint)

The checkpoint's remaining items were not superseded. The three thin remainders were finished first, on
7 November 2026, so that the Stage A rename does not run over half-wired work.

| Checkpoint item | Verdict |
|---|---|
| CrowdSec remainder | **DONE** — it was already complete; the "Traefik plugin block still commented" note was stale (the commented block is the unrelated bot-wrangler placeholder). Verified `helm lint` + `template` with `infra.crowdsec.enabled=true`. |
| agent-document wiring | **DONE** — import smoke, `agents.agentDocument` values block, both OAuth client pairs, guardrail caller allow-list, `infra.llmGateway.callers`, `client-secret-agent-document`, `deploy.sh`, docker-compose + `.env.example`, docs/platform.md |
| PgBouncer auth hardening | **DONE** — and it found a real defect: the `edoburu` entrypoint writes `DB_PASSWORD` into `userlist.txt`, **in cleartext** under `auth_type=scram-sha-256`, so the shipped default would have put the Postgres superuser password in plain text inside the pooler pod. Replaced by an `auth_query` mode (low-privilege lookup role, no application credential in the pod), admin console closed, TLS/startup-parameter settings, container securityContext. docs/impl_pgbouncer.md. Still `enabled: false`; auth_query unvalidated until a live Postgres. |
| Temporal | becomes **Stage C** |
| workflow/artifact tables + skill packages | split: tables into **Stage C**, skill packages into **Stage B** as part of the pack contract |
| ats-scan candidate journey | **after Stage C**, built as a Temporal workflow and as the ATS pack's first end-to-end run |
| admin-ui polish + model management | extend with the pack and ontology tabs from **Stage B** |

---

## 8. Decisions

### 8.1 Settled

1. **`agent-factory` is the platform** — repo, chart, namespace (existing_comment §17). The service at
   `services/platform/agent-factory` is therefore renamed to **`registry-service`**
   (`services/platform/registry-service`, package `registry_service`, DNS/JWT audience `registry-service`,
   secret key `client-secret-registry-service`, Helm values key `registryService`) — decided 2026-11-07. It
   matches the `<what>-service` convention and describes what it holds in the target: packs, skills,
   ontologies, policies, tool catalogue, delegated admin. The rename happens in Stage A.
3. **Order of work** (decided 2026-11-07): finish the 2026-11-06 checkpoint remainders (§7) first, then
   Stage A. Renaming over half-wired work would be wasted effort.
2. **Ontology stays where it is architecturally** (domain-neutral service, caller-supplied schema); only
   ownership of the assets moves into packs.

### 8.2 Open

1. Does `ats-orchestrator` stay a product API with the planner extracted, or does the ATS product become a thin
   UI over a generic intake API plus `ats-mcp`?
2. Broker for priority queues: Temporal task queues with a dispatcher in front, or a separate broker?
3. Stage order: Temporal (C) before or after the pack contract (B)? C unblocks execution and the ats-scan
   journey; B unblocks the second domain.
4. Does the ontology registry serve schemas over an API at run time, or does the pack bundle them and the
   registry only store/approve them?

---

## 9. Explicitly unchanged

LiteLLM gateway and `Purpose` aliases · Temporal as the durable engine · gVisor / NVIDIA sandbox runtime ·
Graphiti knowledge graph on FalkorDB and the ontology model · one Redis 8 with the key registry · KEDA ·
Kubernetes Secrets (pluggable provider) · the single Helm chart and `deploy.sh` · `agent-guardrail` and prompt
safety · the agent-factory authoring plane and `admin-ui` · JWT/JWKS verification with the token and authz
caches · tenant scoping and RLS · OTel/LGTM.

---

## 10. The v3.0 plan and schema — adopt, translate, defer

**Input:** `multi_industry_agent_platform_final_plan_3.0.md` and
`multi_industry_agent_platform_final_schema_3.0.sql` (7 November 2026). The plan is a superset of v2.0: same
spine (LangGraph plans, Temporal executes, MCP services do the work, LiteLLM routes models, KEDA scales) plus
new product surfaces — tenant self-service app builder, ingestion platform, website crawler, NL2SQL/semantic
layer, BYOK model governance, billing, notifications, governance.

### 10.1 The SQL file is a design source, not a DDL source

It is a **fresh-install reference schema** (its own closing note says so) and it disagrees with this repo on
every foundational convention:

| v3.0 schema | this repo today |
|---|---|
| `platform.tenants` -> `platform.workspaces` | `clients` -> `projects` |
| `uuid PRIMARY KEY DEFAULT gen_random_uuid()` | BIGSERIAL `id` for joins + `external_id` UUID on the wire (existing_comment §23) |
| 13 Postgres schemas (`platform`, `iam`, `agents`, `builder`, `ingestion`, `dataops`, ...) | one schema, one Alembic version table (CLAUDE.md §8) |
| GUC `app.tenant_id` | GUC `app.client_id` (`agent_common.db.TENANT_GUC`, `set_tenant_context`) |
| `tenant_id uuid` on every table | `client_id Integer` FK `clients.id` |
| `platform.users` + `tenant_memberships` | `users` + `project_members`, owned by auth-service |
| RLS `ENABLE` + `FORCE` | RLS `ENABLE` only (FORCE is a known TODO: needs a non-owner app role) |

Applying it would mean rewriting the 25 existing tables, every repository, auth-service, the JWT claims, the
`X-Client-Id`/`X-Project-Id` contract, `TenantScope`/`TenantRepository` and every RLS policy. That is a
rewrite, not an extension.

**Therefore: take v3.0's table shapes and relationships, express them in our conventions** — BIGSERIAL `id` +
`external_id_field()`, `client_id`, `AuditMixin`, `enable_tenant_rls`, one schema, a new numbered Alembic
revision (0001 must not be edited now that the platform tables live in it). Read `tenant` as our `client` and
`workspace` as our `project` throughout the v3.0 documents.

### 10.2 Vocabulary map (use this when reading v3.0)

| v3.0 term | this repo |
|---|---|
| tenant | client |
| workspace | project |
| `domain_key` | `Skill.domain` today; the pack id from Stage B |
| service registration | `ToolCatalogue.RegisteredServer` |
| application / use case | not present (v3.0 `builder` surface, deferred) |
| `app.tenant_id` GUC | `app.client_id` GUC |

### 10.3 What v3.0 changes in the stages above

**Stage B is cheaper than it looked, because the mechanism already exists.** The service already has the two
patterns a pack needs:

- `AccessPolicy.document` (JSONB) validated by `PolicyDocument.model_validate` in `operations/policies.py` —
  the one "policy-as-data + strict validator" precedent in the codebase. A `PackDocument` model copies it.
- `SkillVersionFile` (path -> content, sha256 per file, replace-semantics) whose own docstring already
  anticipates "a schema, a rule file, code/*.py". Pack assets — ontology JSON, guidance text, rule files —
  are files on a version, not a new storage mechanism.
- `Skill.domain` is already the de-facto domain grouping and already drives IAM resources (`skill:{domain}/{name}`,
  so `skill:ats/*` policies work today). A pack formalises what `domain` already implies.

**Stage B gains per-project pack enablement.** v3.0 `domain_packs` + `workspace_domains` +
`domain_pack_skills` + `domain_pack_mcp_tools` add something we had not planned: one client running ATS in one
project and insurance in another, with config overrides per project. Adopt that.

**Stage B absorbs the MCP tool registry, and it is a bigger gap than §4.3 said.** `ToolCatalogue` is weaker
than "partial" suggested: its servers come only from the `MCP_REGISTRY` env string, `refresh()` is never
called after startup and only bumps a counter, it never calls `list_tools` on a registered server, and it
stores nothing about any tool's input or output schema. v3.0 `mcp_services` + `mcp_tool_definitions`
(input/output schema, `risk_level`, `requires_approval`, `idempotent`, version) is the missing half, and it is
what lets a plan validator check a DAG without calling live servers.

**`SkillVersion.workflow` is where the plan schema plugs in.** It is an untyped `dict`, stored verbatim and
never schema-checked — today's only "skill definition" payload. Typing it is Stage B's first concrete win.

**Blocking finding (CLOSED 7 Nov — see the end of this bullet): two unconnected skill-version systems.**
The registry authors skill versions as
**database rows** (`Skill`, `SkillVersion`, `SkillVersionFile`, lifecycle draft -> in_review -> published ->
archived, `activate_version` sets `skills.active_version_id` and bumps the Redis `skill:version` counter).
The runtime loads skills as **Python modules** — `agent_skill/skills/<name>/<version>.py`, imported by
`load_skill()` and selected by `DEFAULT_SKILL_VERSION` / a per-request `skillVersion`. **Nothing joins them.**
Publishing and activating a version in admin-ui changes no runtime behaviour today; agent-skill still imports
`ats_screening/v1.py`. Stage B is not complete until the runtime loads the activated version's
`SkillVersionFile` rows (the table's own docstring says content is stored "so a version can be loaded at
runtime without rebuilding an image" — that path was never built). This also decides where ontology and
guidance assets are read from, so it has to be settled first, not last.

**Closed 7 November 2026.** registry-service gained `resolve_skill` — natural key (`domain`, `name`, optional
`versionNumber`) to the version that should run, with every file's content and a content-addressed `cacheId`.
Service callers are gated by `TOOL_POLICY` (`agent-skill=resolve_skill`, default deny for everything else);
user callers need `skill.run`, and a pinned *draft* authorizes against the `skill:<domain>/<name>:draft:<n>`
resource that `set_version_testers` grants, so pinning someone else's draft is refused by the same policy
that governs running it. agent-skill gained `RegistrySkillSource`: it resolves through that tool, caches per
`(client, skill, pin)` keyed on the shared `skill:version` counter — so an activation invalidates every
replica at once without polling — and falls back to the packaged module when the registry cannot answer, so a
registry outage degrades the skill version rather than failing a screening. The file layout it reads
(`skill.md`, `prompts/{jd_analysis,profile_analysis,match,verdict}.md`) is the seed of the pack layout in
docs/impl_multi_domain.md §6.

**Good news on version pinning: it already works end to end.** `Ontology.cache_id` is
`name@version:fingerprint(content)`, and it already feeds both the episode dedup hash
(`content_hash(text, guidance, ontology)`) and the semantic-cache scope (`CacheScope.ontology_id`). Changing
any byte of an ontology re-ingests cleanly and never serves stale answers. Moving ontologies into the registry
does not require inventing a pinning mechanism — only moving where the bytes come from.

**Stage C gains a richer execution model.** We had planned `workflow_runs` + `artifacts` + `artifact_subjects`.
v3.0 proposes `workflow_plans`, `workflow_runs`, `workflow_stage_runs`, `workflow_stage_attempts`, plus the
evidence tables `tool_invocations`, `model_invocations`, `retrieval_events`, `sandbox_executions`,
`execution_events`. That is a better answer to the v2.0 acceptance criterion "every finding carries provenance
and the report is reproducible from pinned versions". Adopt the shape; Temporal stays the source of truth for
history and these are projections (v3.0 §15 agrees).

**Stage F gains a home for the policy.** `tenant_runtime_policies` (default priority, max concurrent
workflows/MCP calls) and `service_capacity_policies` (per client/domain/service priority, concurrency, queue,
replica bounds) are where the priority tier lives — derived server-side, never taken from the client.

**Ontology: ours stays richer.** v3.0 carries only `ontology_version text` on `domain_packs` and
`knowledge_graph_refs`. Our `agent_common.knowledge.ontology` (typed entities/relations/attributes, extraction
instructions, `edges`/`edge_map` authoring, validation, `name@version:content-hash` cache id) is more
complete. Keep it; a pack references a registered ontology version, per §3.

### 10.4 New surfaces v3.0 adds (each is its own phase)

None exist in any form today. These are real product scope, and several answer open items in
docs/existing_comment.md:

| v3.0 surface | Answers | Verdict |
|---|---|---|
| `ingestion` (connectors, data sources, pipeline definitions/versions/runs, cursors, schedules, quality, lineage) | existing_comment §18-20, docs/ats-scan.md | After Stage C — it is a Temporal pipeline |
| `dataops` (schema snapshots, semantic models, NL2SQL request/candidate/approval/execution, read-only query policy) | existing_comment §19-21 asked for exactly this | Own phase; the strict read-only controls in v3.0 §13 are the valuable part |
| `ai` (provider connections, model deployments, LiteLLM mappings, BYOK, routing, guardrail policies) | "model management from UI/DB" (deferred list) | Own phase; LiteLLM stays the gateway, we store references only |
| `agents.service_registrations` (BYO MCP/A2A, hosting mode, verification, exposure approval, access grants) | existing_comment §7 (BYOA) | Own phase; needs egress allow-lists first |
| `builder` (tenant applications, workflow templates, nodes/edges, skill/tool bindings, prompt overrides) | - | Largest new surface; only after a second domain proves the pack model |
| website crawler (`website_source_policies`, approved domains, robots, permission basis) | - | Governed connector; only with the permission model in v3.0 §10 |
| `billing`, `notifications`, `governance` | notifications partly exist (`agent_common.notifications`) | Later |

### 10.5 Cheap hardening from v3.0 worth taking early

Small, fits our conventions, improves what already exists:

1. **Composite tenant-aware foreign keys** (`FOREIGN KEY (client_id, x_id) REFERENCES t(client_id, x_id)`) so
   a valid id from another tenant cannot be attached by an application bug. v3.0 is right that RLS is not
   relational integrity.
2. **Validation triggers** on cross-tenant bindings (v3.0's `validate_*_tenant` pattern).
3. **`FORCE ROW LEVEL SECURITY` + a non-owner application role.** Already our standing TODO; v3.0 §18 makes
   the case. **Measured 7 Nov against a real Postgres** (migrations 0001+0002, two clients): as the table
   OWNER - which is how every service connects today - `SET app.client_id = 1` still returns client 2 rows;
   as a NON-OWNER role the same query returns only client 1, an unset GUC returns nothing (fail closed), and
   inserting a row for another client is refused by the policy. So the RLS design is correct and currently
   inert: tenant isolation rests entirely on the client-scoped repositories, and one missing `client_id`
   filter leaks across tenants. Granting the services a non-owner role is the fix and needs no schema change.
4. **`idempotency_keys` and `outbox_events`** — needed anyway for Stage C idempotency and for notifications.
5. **Append-only audit** (`REVOKE UPDATE, DELETE` for the app role). Note registry-service has `audit.record()`
   but it only emits a log event; there is no audit table yet.

### 10.6 Housekeeping the exploration turned up

Small, unrelated to v3.0, worth fixing when next in these files:

- `ToolSpec.mutating`, `ToolSpec.action` and `ToolSpec.resource` are inert — no code reads them. Either wire
  `mutating` to the audit path or drop it.
- `invoke_remote` duplicates the authorization decision inline instead of going through `authorize()` — a
  second place to change if authorization semantics move.
- `STATE_REJECTED` is declared but never used (`reject_version` returns the version to `draft`).
- `models.is_valid_principal_type` is dead code.
- Several registry-service docstrings reference `docs/impl_registry_service.md`, which does not exist — the
  file is `docs/impl_agent_factory.md`.
- `load_guidance(version)` silently falls back to the default on an unknown version, so a typo in
  `KNOWLEDGE_GUIDANCE_VERSION` ingests every resume under the wrong guidance with no error. It should fail
  loudly, like `load_skill` does for a missing default.
- Knowledge ingest is fire-and-forget: a malformed ontology surfaces as `KNW-INV-0002` in a
  `knowledge.ingest_failed` log line and nothing else. At the product layer a broken ontology is silent.
  Validating the ontology in the registry at publish time (Stage B) is the real fix.
- The ATS carries three unrelated version identifiers for one asset — guidance `"v2"`, file name
  `talent_v2.json`, and the ontology's own `generic_cv_career_graph@2.0` (which is what actually reaches
  `cache_id`). Collapse these to one when the asset moves into the registry.
- The search-time ontology narrowing (`entity_types` from `ontology.entity_names`) is implemented in the
  knowledge service but the ATS never passes an ontology on search, so it never fires. Either use it or drop
  the dead path.

### 10.7 Scope warning

v3.0 is roughly 150 tables across 6 phases. In order it is a multi-quarter programme; attempted at once it is
how a platform ends up half-built. The sequence that keeps every step shippable and verifiable:

```
Stage A  DONE     de-ATS the core
Stage B  next     pack contract + ontology registry + MCP tool registry   <- v3.0 §4, domain_packs, mcp_tool_definitions
Stage C           Temporal + execution projections + evidence tables      <- v3.0 §15, execution.*
Stage D           planner/validator split out of ats-orchestrator
Stage E           shared capabilities out of the product service (rules, vector, blob)
Stage F           priority, fairness, quotas                              <- v3.0 §8, capacity policies
Stage G           MCP-aware edge + gVisor sandbox
Stage H           second domain pack, no core change   <-- the acceptance test
then              ingestion -> NL2SQL -> BYOK models -> BYO MCP -> builder -> billing
```

Stage H is the gate. Until a second pack runs with no change to the orchestration core, building the ingestion
or builder surfaces on top only multiplies what has to be reworked later.
