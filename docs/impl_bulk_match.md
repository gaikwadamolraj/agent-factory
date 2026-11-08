# J3 — bulk match: one criteria set against many subjects

One job description against a thousand candidates; one exclusion clause against open claims; one sanctions
typology against a customer base. Same workflow, different pack (docs/impl_multi_domain.md section 4).
Built 8 November 2026.

## 1. What a loop in a request handler cannot give you

The journey exists for three properties, and each one is a thing that goes wrong without it.

- **It is bounded.** Subjects are selected in pages and graded `concurrency` at a time under a hard
  `max_subjects` ceiling. A bulk run must never be able to open ten thousand concurrent LLM calls.
- **It is resumable.** Each grade is written under `(run, subject)`, so a worker that dies half-way through
  re-grades only what it had not finished. The expensive part is never repeated.
- **One subject's failure is not the run's failure.** A candidate whose resume will not parse is recorded
  with its error code and the run carries on. *A bulk run that silently skipped two hundred subjects looks
  exactly like one that found them unsuitable* — which is the worst outcome available here, because it is
  invisible.

## 2. Shape

```
select_subjects  (paged by id)  ->  match_subject × N  ->  record_match_failure (per failure)
                                                       ->  rank_results  ->  shortlist in the run summary
```

- **`match_results`** (migration `0004`) holds one row per subject: score, band, recommendation,
  per-criterion grades, the rubric that produced them, or the error code that stopped it. The run's
  `result_summary` carries only the shortlist and the counts, because it is a small JSON column and workflow
  history is not a database.
- **`score` is `Decimal`, not `float`.** It is ordered, compared and shown to people, and a ranking that
  depends on binary floating point is a ranking nobody can reproduce.
- **Ranking is one SQL statement**, not a round trip per row: a thousand results ranked row by row would
  take longer than grading them did.

### Paging is keyset, not OFFSET

`SubjectRepository.page_for_type` orders by id and takes `after_id`. A bulk run can take an hour, and OFFSET
over a table someone is still inserting into silently skips rows and repeats others — candidates would go
ungraded with nothing to show for it.

### The only ATS-specific file

`workflows/bulk_adapters.py`. It knows the criteria set is a job description, the subject is a candidate, and
grading is a screening. It deliberately calls the **same** agent and skill version an interactive screening
uses: a shortlist a recruiter cannot reproduce by screening one candidate by hand would be worthless, and two
scoring paths would drift the moment either changed.

Each subject is graded under its own agent session id, so one candidate's result can never depend on the
candidate graded before them.

## 3. Its own queue, and why that is not a detail

Bulk match runs on the `bulk-match` task queue with its own workers and its own KEDA ScaledObject; onboarding
keeps `onboarding`. One image and one entrypoint (`python -m ats_orchestrator.worker`), selected by
`WORKER_QUEUE`, so each Deployment scales on its own backlog.

A thousand-candidate run must never delay one recruiter's upload, and a **separate queue is the only thing
that actually guarantees it**. A shared queue with priority ordering still shares workers, and the bulk run
holds every slot for an hour. Runs are also tagged `priority_tier: "bulk"` on `workflow_runs`, which is what
a future scheduler will read — but the queue split is what makes the guarantee today.

The two queues scale in opposite shapes: onboarding is many short runs (`targetQueryValue: 4`,
`maxReplicaCount: 3`); bulk is few long ones (`targetQueryValue: 1`, `maxReplicaCount: 2`).

### What the bulk worker is allowed to reach

Its own OAuth identity (`ats-orchestrator-bulk-match`) with the narrowest grant that works:

| Allowed | Not allowed |
|---|---|
| agent-skill, tool `screen_candidate` only | agent-knowledge-graph, agent-guardrail, agent-compute |
| registry-service, tool `resolve_skill` | `ask_agent`, `cancel_request` |
| Temporal, Postgres | **the LLM gateway** |

The last one is the easy mistake. This worker *looks* like it needs model access because it grades
candidates — but it selects them with a SQL query and grades them by calling agent-skill, which holds the
gateway key and makes every model call. It gets no `LLM_GATEWAY_KEY` and no `llm-config`: a credential a
process cannot use is a credential it should not be able to leak. It also gets its own ConfigMap rather than
the API's, so it is never handed the knowledge-graph, guardrail or compute addresses its grant already
denies.

## 4. Verified, 8 November 2026

Real Temporal, real Postgres, 25 subjects, two of them rigged to fail. The grader is the one thing stubbed —
the point of the run is the journey, not whether an LLM scores a resume well.

| Check | Result |
|---|---|
| Every subject selected and graded | 25 selected, 23 graded, 2 failed |
| The run lands on its own queue and tier | `task_queue=bulk-match`, `priority_tier=bulk` |
| One row per subject in `match_results` | 25 rows; stored counts agree with the run summary |
| Failures are recorded, never dropped | both failing subjects present with their error code |
| The shortlist is ranked and capped at `top_n` | 5 entries, `[97.5, 95.0, 92.5, 90.0, 87.5]` |
| Ranks are dense from 1 | `1, 2, 3, …` |
| A clean subject is graded exactly once | 1 call each |
| A transient failure is retried to the policy maximum | 3 calls each, recorded `WFL-INT-0001` |
| A subject that *cannot* be graded | **1 call**, recorded `SCR-MIS-0003` |
| Re-submitting the same job description | reattaches to the one run and **re-grades nobody** |

The last three are the ones worth keeping. Retrying is right for a failure that might fix itself and wrong
for one that cannot: a missing job description retried three times across a thousand subjects is an hour
spent learning nothing, so the grader's "this cannot be graded as asked" is raised non-retryably with its
own code. And a reattach costs nothing. All measured, not assumed.

## 5. Open

- **No producer in the product yet.** `journeys.start_bulk_match` exists and is verified, but no endpoint or
  UI calls it. Unlike onboarding there is deliberately **no in-process fallback**: grading a thousand
  candidates inside a request is not a degraded version of this journey, so a caller without an engine must
  say bulk matching is unavailable rather than quietly attempting it.
- No progress endpoint. The workflow answers a `progress` query (`selected`/`graded`/`failed`) and
  `/api/v1/runs/{id}` shows the row, but nothing streams progress while a run is going.
- `rank` is assigned at the end of the run only, so a shortlist read mid-run is ordered by score but
  unnumbered.
- Not measured at scale: 25 subjects, not 1,000. The concurrency window and the page size are settings, and
  the first real run should be watched rather than trusted.
