# Multi-domain architecture and the journey catalogue

**Status:** proposed. The architectural answer to "how do ATS, insurance, banking and media share one platform".
**Date:** 7 November 2026
**Reads with:** docs/impl_domain_platform.md (stages A-H and the v2.0/v3.0 reconciliation). This document is the
*domain* design; that one is the *platform* design. Billing and admin surfaces are deliberately out of scope here.

---

## 1. The question this answers

v3.0 says: one MCP service per domain (`ats-mcp`, `insurance-mcp`, `banking-mcp`, `media-mcp`), each exposing
its own tools, all sharing document/KG/vector/sandbox/rules services underneath.

That is the right *deployment* shape and the wrong *implementation* shape, because it quietly asks us to write
matching, scoring, grounding and reporting four times — once per domain, each slightly different, each with its
own bugs. Four years from now that is four codebases with a shared logo.

This document proposes the sharper boundary: **what is genuinely domain-specific is small, declarative and
belongs in a pack. What looks domain-specific is usually the same operation with different vocabulary, and
belongs in a shared capability.**

---

## 2. The core insight: every journey is Criteria x Subject -> Evidence -> Verdict

Look at the four domains as flows rather than as products:

| Domain | Subject | Criteria set | Verdict |
|---|---|---|---|
| ATS | candidate | job description requirements | fit score + recommendation |
| Insurance | claim | policy coverage, exclusions, conditions | covered / not / needs review + evidence |
| Banking (KYC) | customer | KYC and AML policy controls | risk summary + evidence pack |
| Banking (AML triage) | alert | typology rules | triage recommendation |
| Media | brief / audience | brief requirements, compliance rules | plan fit + compliance findings |
| Healthcare (later) | case | clinical guideline | evidence completeness |

They are the same operation: **take a subject, take a set of criteria, find evidence for or against each
criterion, grade it, score deterministically, and produce a report where every claim is traceable to a source.**

### 2.1 We have already built this engine — for one domain

`services/agents/agent-skill/src/agent_skill/screening/` is exactly that engine:

| Step | File | What it generically is |
|---|---|---|
| `analyze_job_description` | `steps.py:90` | parse the **criteria set** from a source document |
| `analyze_candidate_profile` | `steps.py:113` | parse the **subject record** from a source document |
| `match_requirements` | `steps.py:164` | for each criterion, grade the subject's evidence |
| `write_verdict` | `steps.py:216` | render the **report** |
| `compute_score` | `scoring.py:72` | deterministic weighted score from the grades |
| `verify_jd` / `verify_profile` / `verify_matches` | `grounding.py:48,70,105` | every claim must be quotable from source text |
| `flag_injection` | `steps.py:137` | a subject document that tries to instruct the AI is always flagged |

And the grade vocabulary is **already domain-neutral**:

```python
GRADE_WEIGHT = {"strong": 1.0, "moderate": 0.6, "adjacent": 0.4,
                "claimed": 0.25, "invalidated": 0.0, "absent": 0.0}
```

`claimed` = asserted but not evidenced. `invalidated` = contradicted by evidence. `absent` = not found. Those
read perfectly as *is this loss covered*, *is proof of address present*, *does this audience meet the brief*.
Nothing about that vocabulary is about recruitment.

What is ATS-specific is only: the words ("job description", "candidate"), the criteria schema, the weights, the
report template, and `scoring.py`'s date-interval maths for years-of-experience.

### 2.2 The proposal

Promote this to a **domain-neutral `agent-match` capability**, and let the pack supply the vocabulary:

```
agent-match  (shared capability, no domain knowledge)
  extract_criteria(source, criteria_schema, guidance)   -> CriteriaSet
  extract_subject(source, subject_schema, guidance)     -> SubjectRecord
  grade_criteria(CriteriaSet, SubjectRecord, evidence)  -> [CriterionResult{grade, evidence_refs}]
  render_report(results, template)                      -> Report

agent-rules  (shared capability, deterministic, no LLM)
  score(results, rubric_version)                        -> score, band, recommendation
  check_thresholds(score, rubric)                       -> pass/fail/needs_review

grounding    (in agent-match, deterministic)
  every grade must cite a span in a stored source; ungrounded grades are downgraded to `claimed`
```

A domain MCP service then holds only what is *actually* domain-specific — `identify_fraud_indicators`,
`estimate_audience_count`, `resolve_customer_entities`. Those are thin. The thick part is shared.

**Why this beats one-service-per-domain:** adding insurance becomes "write a pack" (criteria schema, ontology,
rubric, prompts, report template) plus two or three genuinely novel tools — not "implement match/score/ground/
report again". It also means a grounding bug is fixed once for every regulated domain at the same time.

---

## 3. One subject model instead of a table per vertical

The knowledge graph already has the right abstraction: `subject_ref` ("who this text is about", e.g.
`document:42`), with `subject_refs` on search to narrow to one subject's facts without splitting the graph —
which is what keeps cross-subject matching possible.

Generalise that into the application database, as v3.0's `business_records` does, in our conventions:

```
subjects            client_id, project_id, domain, subject_type, external_ref, display_name,
                    status, schema_version, classification, payload JSONB, search_metadata JSONB,
                    + external_id + AuditMixin + RLS
subject_versions    immutable payload snapshots (who changed what, why)
subject_relations   subject -> subject, typed (candidate->application, claim->policy, customer->account)
```

`subject_type` is pack-defined: `candidate`, `claim`, `policy`, `customer`, `kyc_case`, `brief`, `campaign`.
High-value filter columns stay relational; everything variable lives in `payload`. One GIN index on `payload`.

This is the single most important schema decision in the whole plan. A `candidates` table, then a `claims`
table, then a `customers` table is how the platform becomes four platforms.

`subjects.external_ref` is what the KG's `subject_ref` points at, so the graph and the record stay joined.

---

## 4. The journey catalogue

Six journeys. Every one is domain-neutral; a pack instantiates it. This is the product surface.

### J1 — Onboard a subject
Ingest one or more source documents, produce a `subjects` row, index it, enrich the graph.
`acquire -> land -> parse -> classify -> injection-scan -> extract to subject payload -> chunk+embed -> KG episode -> validate -> publish`

| Domain | Instantiation |
|---|---|
| ATS | resume -> candidate |
| Insurance | policy schedule -> policy; claim bundle -> claim |
| Banking | ID documents + proofs -> customer / kyc_case |
| Media | brief document -> brief |

### J2 — Match a subject against a criteria set
The CEV engine of §2. Criteria come from a document (JD, policy wording, control list, brief) or from a stored
criteria set.

| Domain | Instantiation |
|---|---|
| ATS | candidate vs JD -> fit score |
| Insurance | claim vs coverage + exclusions -> covered / review |
| Banking | customer vs KYC control list -> evidence gaps |
| Media | audience vs brief -> plan fit |

### J3 — Bulk match (one criteria set vs N subjects)
The same engine, fanned out, ranked, top-N returned. This is the journey with the strongest commercial pull and
the one that most needs KEDA and the priority scheduler.

| Domain | Instantiation |
|---|---|
| ATS | one JD vs 1,000 candidates -> shortlist |
| Insurance | one exclusion clause vs open claims -> exposure review |
| Banking | one new sanctions typology vs customer base -> rescreen |
| Media | one brief vs audience segment library -> candidate plans |

### J4 — Ask about one subject (grounded Q&A)
Chat scoped to a subject: its documents, its graph facts, its match runs. Answers cite evidence. This already
exists for ATS (`docs/journeys.md` J3) and is domain-neutral apart from guidance text.

### J5 — Ask across subjects
Graph and vector retrieval across the project; later, governed NL2SQL over `subjects.payload` and connected
customer databases (v3.0 `dataops`, existing_comment §19-21). **Read-only, allow-listed, parameterised,
row-capped** — prompting is not a control.

### J6 — Review and decide
Human approval on a run outcome. Mandatory for insurance claim decisions, banking consequential decisions and
anything clinical; optional for ATS. The platform prepares evidence and a recommendation; a person decides.
Decision and provenance are recorded.

**J1-J4 are the MVP of any new domain.** J5 and J6 are what make the regulated ones sellable.

---

## 5. One ingestion pipeline, pack-supplied profiles

The user-visible ask is "data ingestion pipelines". The design discipline is: **one pipeline shape, many
profiles** — never a pipeline per domain.

```
 acquire ──► land ──► parse ──► classify ──► scan ──► extract ──► index ──► enrich ──► validate ──► publish
   │          │        │          │           │         │          │          │           │           │
 connector  blob     agent-    doc type   guardrail   pack      chunks +   KG episode  quality    subject
 (upload,   store    document  (pack       (prompt    extraction embeddings (pack       rules      row +
  S3, SFTP,          (pdf/docx/ profile)   injection) profile +  (vector)   ontology)   (pack)     events
  SharePoint,         xlsx)                           schema)
  DB, API,
  website)
```

Every stage is a Temporal activity. The **pack** decides: which connectors are allowed, the document
classification profile, the extraction schema, the ontology + guidance, chunking parameters, the quality rules,
and the target `subject_type`. The **platform** owns the stages, retries, idempotency, cursors, checkpoints,
lineage and scaling.

Concretely this means a resume, a policy PDF, a bank statement and a media brief run **the same code path**.
That is the test of whether the abstraction is real.

Supporting tables (v3.0 shapes, our conventions): `data_sources`, `pipeline_definitions`, `pipeline_versions`,
`pipeline_runs`, `pipeline_stage_runs`, `source_cursors`, `ingested_items`, `data_quality_results`,
`data_lineage_events`.

Notes that matter:
- **Idempotency is the whole game.** Content hash per item + `idempotency_key` per run stage; re-running a
  pipeline must not duplicate subjects or re-pay for extraction. The KG already does this well
  (`content_hash(text, guidance, ontology.cache_id)`) — copy the pattern.
- **Per-source throttling**, not just per-service: one tenant's SharePoint sync must not starve the queue.
- **Website crawling is a governed connector**, never an open browsing agent: approved domains, permission
  basis, robots, rate limits, retention (v3.0 §10). Build it last.
- **Tabular sources** (CSV, RDBMS) land as `subjects` + typed rows, then answer questions through governed
  NL2SQL — not by stuffing a spreadsheet into a prompt (existing_comment §19-21).

---

## 6. What a domain pack actually contains

Not an abstraction — a directory of files, versioned and approved like a skill version, stored as
`SkillVersionFile` rows (that table already stores path -> content + sha256, and its docstring already
anticipates "a schema, a rule file, code/*.py").

```
packs/insurance/2.0/
  pack.json                 identity, version, enabled journeys, required capabilities, data policy,
                            human-approval list, execution profile (priority, max parallel, checkpoints)
  ontology.json             entity/relation/attribute schema  (agent_common.knowledge.ontology)
  guidance/
    ingest.md               what to extract, what never to infer, treat text as data
    answer.md               tone and focus for J4
  subjects/
    claim.schema.json       subject payload schema -> subjects.payload
    policy.schema.json
  criteria/
    coverage.schema.json    the criteria set shape for J2
  rubrics/
    coverage.rubric.json    grade weights, thresholds, bands, recommendation mapping (deterministic)
  prompts/
    extract_criteria.md     {{PLACEHOLDER}} templates, single-pass render
    extract_subject.md
    grade.md
  reports/
    assessment.md.j2        report template
  tools.json                MCP services + allowed tools this pack may call
```

**Built 7 Nov** as `agent_common.pack` (`PackDocument` + `parse_pack` + `validate_pack`, errors `PCK-*`),
following the `AccessPolicy.document` + `PolicyDocument.model_validate` pattern. registry-service validates
any version carrying a `pack.json` on every file write and again at submit-to-review, so an invalid pack is
refused at authoring time instead of failing later on a user request. Verified refusals: executable asset
types (.py/.sh/.js/.exe), path traversal, absolute and backslash paths, wildcard tool grants, unknown or
duplicate journeys, invalid data classification, self-promoted priority tier, unknown fields, duplicate
subject/criteria/rubric keys, and references to files the version does not contain.

**Nothing in a pack is executable.** Prompts, schemas, rubrics and templates only. Anything that needs real
code goes to the sandbox as an approved, scanned, versioned script (v3.0 §5's "safe self-service boundary").

---

## 7. Worked example: the same journey, four domains

J2 (match), with only pack data differing:

| | ATS | Insurance | Banking KYC | Media |
|---|---|---|---|---|
| criteria source | JD document | policy wording + exclusions | control list | brief |
| criteria schema | `requirement[]` | `coverage_clause[]` | `control[]` | `requirement[]` |
| subject | candidate | claim | customer | audience segment |
| evidence | resume text + KG facts | claim docs + policy graph | ID docs + transactions | research + data profiles |
| grades | same six | same six | same six | same six |
| rubric | skills weighted, years matter | clause-level, any exclusion is decisive | all mandatory controls must be `strong` | weighted fit |
| verdict | fit score + recommendation | covered / review, never auto-decline | evidence pack, never auto-reject | plan fit |
| human approval | optional | **required** | **required** | optional |

The rubric differences are real and they are *data*: "any exclusion is decisive" is a threshold rule, not code.
That is why `agent-rules` is deterministic and versioned, and why no LLM ever computes the score
(existing_comment §28 asks for exactly this, and CLAUDE.md forbids LLM arithmetic).

---

## 8. What this changes in the code we have

| Today | Becomes | Stage |
|---|---|---|
| `agent-skill/screening/steps.py` (ATS-shaped) | `agent-match` capability, pack-driven vocabulary | E — partly done |
| `agent-skill/screening/scoring.py` (hard-coded weights) | `agent-rules`, rubric from the pack | E — **done 8 Nov** |
| `agent-skill/screening/grounding.py` (hard-coded) | grounding in `agent-match`, rules from the pack | E — **done 8 Nov** |
| `ats_orchestrator/knowledge/{guidance.py, ontologies/}` | pack assets in the registry | B |
| `skills/<name>/<version>.py` Python modules | pack files loaded at runtime from the registry | B (see the blocker) |
| `screenings` table | `match_runs` over `subjects` + `artifacts` | C |
| documents/chunks in `ats-orchestrator` | shared ingest + index capability | E |
| `ats-orchestrator` LangGraph assistant | domain-neutral planner + ATS product API | D |

**The blocker named in docs/impl_domain_platform.md §10.3 is now CLOSED (7 Nov).** registry-service exposes
`resolve_skill` (natural key -> the version that should run, with its files and a content `cacheId`), and
agent-skill resolves through it before falling back to the packaged module. Activating a version in the admin
UI now changes what the agent executes. The file layout it reads (`skill.md`, `prompts/*.md`) is the seed of
the pack layout in §6.

---

## 9. Build order

Phrased as journeys, since that is how the value lands.

1. **Runtime pack loading** (Stage B, first) — **core DONE 7 Nov**: `resolve_skill` on registry-service and
   `RegistrySkillSource` in agent-skill, so an activated version is what the agent executes; and the pack
   contract (`agent_common.pack`, enforced by registry-service on write and at submit). Still open in this
   step: moving the ATS ontology and guidance out of `ats-orchestrator` into pack files, which needs the
   orchestrator to read them from the registry instead of from module constants.
2. **`subjects` model** — **DONE 7 Nov.** `subjects` + `subject_versions` + `subject_relations` in
   `agent_common.db.platform_tables` with repositories, migration `0002_subjects`, RLS, a GIN index on
   `payload`, and a `graph_ref` property (`<subject_type>:<external_ref>`) that is exactly what the knowledge
   graph stores as `subject_ref`. Verified against a real Postgres: a candidate and a claim coexist in one
   table, the natural key and the no-self-relation check reject bad writes, JSONB filtering works, and
   downgrade/re-upgrade is clean. Still to do: migrate the ATS candidates onto it.
3. **Temporal + J1 for ATS** (Stage C) — the onboarding pipeline as the first real workflow; this is
   docs/impl_ats_scan.md, built generically. **DONE 8 Nov** — `onboard_subject` with its seven stages, the
   `workflow_runs` projection (launch, lifecycle, cancellation), an onboarding worker scaled by KEDA on the
   queue's backlog, and the engine optional throughout: with no `TEMPORAL_ADDRESS` the orchestrator keeps its
   in-process path. Verified end to end against a real engine and a real database (docs/impl_temporal.md §6).
   The producer is the resume upload (`ats_orchestrator/journeys.py`): it hands the candidate - not the turn -
   to the durable run, keyed on the resume, and returns `None` so the caller keeps its in-process background
   ingest whenever the engine is absent or unreachable. The activities are domain-neutral — `adapters.py` and
   `journeys.py` are the only ATS-specific files, which is exactly the seam the insurance pack (step 6) will
   be judged on.
4. **Generalise the engine** (Stage E) — `agent-match` + `agent-rules`. **DONE 8 Nov** as shared *libraries*
   rather than services: neither has data or a runtime of its own, so CLAUDE.md's service-vs-lib rule puts
   them in `agent_common` (`agent_common.match`, `agent_common.rules`), and grading a resume never crosses
   the wire a second time. Recruitment's half - date arithmetic, the JD/resume vocabulary, the correction
   wording a recruiter reads - stays in agent-skill. The weights, caps and bands that were hard-coded in
   `screening/scoring.py` are now the ATS pack's `rubrics/fit.rubric.json`. Verified identical on 60 pinned
   scoring cases and 1,201 grounding comparisons: see docs/impl_match_rules.md.
5. **J3 bulk match for ATS** — the commercially strongest journey. **DONE 8 Nov** — `bulk_match` workflow,
   `match_results` (migration 0004), its own `bulk-match` task queue, worker and KEDA ScaledObject, and the
   `start_bulk_match` producer. Verified end to end on 25 subjects including failure isolation and retry
   behaviour: docs/impl_bulk_match.md. The queue split is what keeps a bulk run from delaying an upload; a
   full priority scheduler (Stage F) is still open, and `priority_tier` is already recorded for it.
6. **Second pack: insurance** (Stage H) — **DONE 8 Nov** as `packs/insurance/v1/` (12 files, validates with
   the same `validate_pack` registry-service runs). The acceptance test passed where it counts: insurance
   brings its own five-grade vocabulary sharing **nothing** with recruitment's six, "any exclusion is
   decisive" is a cap in the rubric rather than code, and the same `agent_common.rules` and
   `agent_common.match` run both domains in one process unchanged. What it does **not** yet prove: no
   insurance grader adapter exists, so J1/J2/J3 have not run end to end on a real claim, and the pack has
   not been published through registry-service. See docs/impl_insurance_pack.md.
7. **Ingestion connectors** — beyond upload: S3/SharePoint/DB, cursors, schedules.
8. **J5 / NL2SQL**, then banking, then media, then the crawler.

Insurance before banking and media on purpose: it exercises human approval and evidence provenance (the
regulated-domain muscles) without banking's sanctions and PII weight.

---

## 10. Honest limits

- **The CEV abstraction will not fit everything.** Media audience *estimation* is a calculation, not a grading;
  banking entity resolution is a matching problem of a different kind. Those stay as domain tools. The claim is
  that the dominant journey in each domain fits, not every operation.
- **Four grades is a modelling choice.** If insurance needs "covered / excluded / conditional / insufficient
  evidence", that is a pack-level grade vocabulary, not a code change — but it does mean the grade set must be
  pack data from the start, not a Python enum. Design it that way now.
- **Regulated domains are not unlocked by architecture.** Shared infrastructure with RLS is not automatically
  acceptable for banking or healthcare data; isolation tier, residency, retention and audit are per-customer
  commitments (v3.0 §4). Do not promise a regulated domain on the shared tier.
- **This is a multi-quarter plan.** Steps 1-5 are the platform becoming real. Step 6 proves it. Everything
  after step 6 is additive, which is the whole point of doing 1-6 in that order.
