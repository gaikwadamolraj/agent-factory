# Agent platform: standalone agents, skill packages, workflow runs and artifacts (DESIGN - for review)

Status: proposal, nothing built yet. Goal: ATS is the first *domain*; insurance (or any other) is added by
writing skill packages, not by writing a new service.

## 1. Naming: every reusable agent is a standalone `agent-<name>` service

Convention: folder = deployment = Service DNS name = service JWT audience = `agent-<what it does>`; Python
package = the same with `_`. Domain-specific agents carry the domain: `agent-<domain>-<what>`. Singular.

Status of the rename: applied in `services/` (folders, Python packages, `service_name` defaults, caller
allow-lists, JWT audiences in `agent_common.constants`), `scripts/` and `docs/`; `deploy/` follows separately.
The left column below is kept as the record of the old names.

| Today (folder / deployment)                    | New name                          | Role (domain-free unless named)                        |
|------------------------------------------------|-----------------------------------|--------------------------------------------------------|
| `services/orchestrator` / `ats-orchestrator`   | `ats-orchestrator`          | ATS app: API, auth, chat assistant, calls the agents   |
| `services/agent` / `ats-agent`                 | `agent-skill`                     | Runs skill packages (Strands); ATS screening is a skill |
| `services/compute` / `ats-compute`             | `agent-compute`                   | Runs code another agent wrote, through the sandbox     |
| `services/sandbox` / `ats-sandbox`             | `agent-sandbox`                   | Isolated code execution (no network, no secrets)       |
| `services/knowledge` / `ats-knowledge`         | `agent-knowledge-graph`           | Knowledge graph API: store text as facts, search, answer |
| `ats-knowledge-worker` (same code)             | `agent-knowledge-graph-worker`    | Graph extraction worker (LLM -> entities and facts)    |
| `services/agents/agent-guardrail`                     | unchanged                         | Injection / safety checks for any text                 |
| new                                            | `agent-document`                  | Reads PDF / Word / Excel -> text, tables, chunks       |
| `services/ats/ats-ui` / `ats-ui`           | unchanged (`ats-ui`)        | ATS UI (an app, not an agent)                          |
| `services/platform/schema`, `services/common`           | unchanged                         | Schema job; shared library `agent_common`                |

Namespaces: `agent-infra` = shared stores and platform plumbing (FalkorDB, graph Redis, LLM gateway,
observability); `agent-factory` = agents and apps. Postgres and the main Redis stay where they are (namespace `amol`).

Rules: agents never import each other (MCP only); anything two agents need goes to `services/common`
(`agent_common`: MCP server scaffold, service auth, tool policy, guardrails client, LLM, workflow engine below).

## 2. Skill packages (domain = a folder of skills)

```
skills/
  ats/                                   # domain
    screening/v1/
      skill.yaml                         # name, version, description, inputs/outputs JSON schema, workflow
      SKILL.md                           # instructions the agent follows when the skill is active
      prompts/jd_analysis.md ...         # prompt templates
      schemas/requirements.json ...      # output schema per LLM step (validated in code)
      rules/scoring.json                 # domain rules / rubric as data
      code/score.py                      # deterministic steps, run in agent-sandbox
      tests/golden/*.json                # input -> expected output, run on release
  insurance/
    claim_check/v1/ ...                  # same shape, different files
```

`skill.yaml` workflow steps (one engine, four step kinds):

```yaml
workflow:
  - id: requirements
    kind: llm                 # prompt + output schema -> JSON (agent_common.llm, purpose alias)
    prompt: prompts/jd_analysis.md
    output: schemas/requirements.json
  - id: document
    kind: tool                # MCP call to another agent, e.g. agent-document.read / agent-knowledge.search
    tool: agent-document.read
  - id: score
    kind: code                # code/score.py in agent-sandbox: JSON in on stdin, JSON out on stdout
    code: code/score.py
    inputs: [requirements, match, rules/scoring.json]
  - id: guard
    kind: guard               # agent-guardrail check on untrusted inputs
```

* Released versions are immutable (CLAUDE.md section 10). Packages are synced into a `skill_packages` table
  (content hash, files as JSON) so a new domain or version is loaded at runtime **without rebuilding images**; the
  loader interface stays the same whether files come from the repo or the DB.
* `code` steps run in agent-sandbox only (standard library, no network, time/memory limits), so skill code is
  dynamic but cannot reach secrets or other services.
* Scores and decisions still come from code/rules, never from the LLM alone (docs/security_llm.md).

## 3. Common tables: workflow runs and artifacts (reuse instead of repeating calls)

All tenant-owned (`client_id`, `project_id` first in indexes), created by Alembic in `services/platform/schema`.

* `workflow_runs`: id, tenant, user_id, skill (domain/name), skill_version, skill_hash, input_hash, status
  (queued/running/done/failed/cancelled), parent_run_id, request_id, error_code, tokens, started_at, finished_at.
* `workflow_steps`: run_id, step_id, kind, status, attempt, input_hash, artifact_id, model alias, duration_ms.
* `artifacts`: id, tenant, run_id, step_id, name, kind (json / text / file), mime, content (jsonb) or blob_key,
  content_hash, size, created_at, expires_at.
* `artifact_subjects`: artifact_id, tenant, subject_type (document / candidate / claim ...), subject_id; this is
  how "everything known about this resume" is found.

Reuse:
1. **Before a run:** same `skill_hash + input_hash` already done -> return its artifacts (no LLM call).
2. **Before a step:** same step + `input_hash` -> reuse the step artifact (a retry resumes, like knowledge sections).
3. **Chat questions:** the assistant first looks up artifacts for the chat's subjects (e.g. the screening's
   requirements, matches, verdict) and answers from them; it calls a skill only if nothing answers the question.
   Artifacts are fenced as untrusted data like any document text.

The existing `screenings` table becomes a view over runs + artifacts (kept for the UI), then retired.

## 4. Phases (each deployable on its own)

1. Rename to `agent-*` + switch to Helm (one restart).
2. Tables + workflow engine in `agent_common.workflow` (framework-free) + artifact reuse in the chat.
3. Skill package format + loader (repo and DB); ATS screening moved to `skills/ats/screening/v2`
   (v1 kept as is). Golden tests.
4. `agent-document` (PDF / Word / Excel) used as a `tool` step.
5. A small `skills/insurance/...` sample to prove a second domain needs no new service.

## 5. Why the full restructure (what each piece unlocks)

The restructure is not cosmetic. It turns one ATS application into a **platform**: a set of reusable, independently
scalable services that share one library (`agent_common`) and one set of DB conventions. We pay the move cost once,
now, while nothing is deployed and the DB is fresh, so that every item on the roadmap below becomes an **add**, not
a rewrite. The grouping states each thing's role: `ats/` = one product; `agents/` = reusable, domain-free agents
any product calls; `platform/` = cross-cutting services (auth, schema, agent-factory, admin-ui); `common/` = the
shared code and data conventions. This is the "common services and DB" the wider vision needs (a second product,
e.g. the red-team engine, sits on the same foundation without forking it).

How the structure we are putting in place now maps to each deferred capability:

| Deferred capability | What the restructure puts in place so it is a drop-in later |
|---|---|
| **Temporal** (durable workflows for skills + extraction) | Every agent is already a standalone deployment behind MCP with its own queue; `workflow_runs` / `artifacts` tables are designed now; KEDA scales per queue. Temporal becomes the engine that drives steps across these agents - no agent rewrite. |
| **OPA** (policy as a service) | `agent_common.policy` is an evaluator behind an interface, with `access_policies` as the source of truth. Swapping/augmenting the in-code backend with OPA touches one package, not every caller. |
| **A2A + bring-your-own-agent** | `agents/` makes internal and external agents peers behind one MCP/`AgentServer` contract; an agent registry + egress allow-list slot in; skills reference capabilities, not URLs, so a customer agent can serve a step. |
| **agent-document** (PDF/Word/Excel) | Just another service in `agents/` using the shared server scaffold; wired as a skill `tool` step. The home and the contract already exist. |
| **Pluggable secrets** (OpenBao/GCP/AWS/Azure) | `agent_common.secrets` is one `get_secret()` interface; the backend is config per client. New product, same interface. |
| **Shared multi-tenant data** (RLS, id split, audit mixin, async SQLModel, PgBouncer) | New tables adopt the conventions now and live in `common/`; the shared DB layer is what lets another product reuse the schema and the connection/pooling choices in one place. |
| **Langfuse / deeper observability** | OTLP is already centralized in `agent_common.telemetry`; Langfuse is an added exporter, not a per-service change. |
| **Model management from UI/DB** | agent-factory + admin-ui are the admin plane the restructure creates; model config becomes one more admin surface there. |

In short: the renames give every service one unambiguous identity (DNS = deployment = JWT audience = folder); the
grouping gives independent scaling and clear ownership; and `common/` is the single place the whole roadmap plugs
into. Without this, each roadmap item would mean touching the monolith again; with it, each is a new service in the
right folder or a new backend behind an existing interface.
