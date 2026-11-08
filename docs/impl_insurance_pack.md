# The insurance pack — the acceptance test

docs/impl_multi_domain.md §9 step 6 calls a second domain "the acceptance test for everything above". The
claim being tested is precise:

> The rubric differences are real and they are *data*: "any exclusion is decisive" is a threshold rule, not
> code.

If that is true, a second industry is a directory of files. If it is false, it is a fork. Built 8 November
2026 as `packs/insurance/v1/`.

## 1. What had to be true

| Claim | How it was tested |
|---|---|
| A pack can bring its **own grade vocabulary** | insurance uses `covered / conditional / excluded / insufficient_evidence / not_applicable` — **0 names shared with recruitment's six** |
| "Any exclusion is decisive" is **data** | a cap with `minFraction: 0` over `clause_type: exclusion`, `maxScore: 0` |
| The shared scoring engine needs **no change** | the same `agent_common.rules.score` runs both rubrics in one process |
| Grounding is **vocabulary-free** | the same `verify_criteria` / `verify_grades`, driven by an insurance `GroundingPolicy` |
| Recruitment is **unaffected** | the ATS rubric still scores its pinned case identically |

## 2. The pack

Twelve files, no code — `pack.json`, `ontology.json` (14 entity types, 24 edges), two subject schemas
(`claim`, `policy`), `criteria/coverage.schema.json`, `rubrics/coverage.rubric.json`, two guidance files,
three prompts and a report template. It validates with the same `validate_pack` registry-service runs on
publish:

```
VALID: pack insurance@v1 - 12 files, journeys: onboard_subject, match_subject, bulk_match, ask_subject, review
subject types: ['claim', 'policy']   criteria: ['coverage_clauses']   rubrics: [('coverage', 'coverage_clauses')]
human approval required for: ['match_subject', 'bulk_match']   classification: restricted
```

## 3. Results

```
a clean covered claim                score=100.0  Covered                  Assess for settlement
ANY exclusion is decisive            score=  0.0  Not covered              capped: an exclusion applies: Flood
an exclusion that does not apply     score=100.0  Covered                  (unchanged)
a condition precedent not satisfied  score= 25.0  Needs review             capped: condition precedent ...
no insuring clause responds          score= 30.0  Needs review             capped: no insuring clause responds
covered but conditional              score= 65.0  Covered with conditions  Refer to an assessor
mostly unevidenced                   deduction = exactly its 10 points (93.3 -> 70.0)
no clauses could be read             score=  0.0  Not covered              the pack's own reason
```

Grounding, with the pack's words rather than recruitment's:

```
clauses kept: ['Accidental damage', 'Flood']   invented and dropped: ['Terrorism']
an uncited 'covered' became 'insufficient_evidence'
correction: "Accidental damage: covered -> insufficient_evidence (nothing in the claim file evidences it)"
```

**Not one line of `agent_common.rules` or `agent_common.match` was changed to make this work.** The engine
never learns what an exclusion is; it matches an attribute and applies a cap.

## 4. Where insurance deliberately differs

- **A decline is never automatic.** Every band's recommendation refers to a person, and the worst possible
  outcome still reads *"Refer to an assessor before any decline"*. `humanApprovalRequiredFor` lists
  `match_subject` and `bulk_match`, and `dataPolicy.classification` is `restricted`, not `confidential`.
- **Two subject types**, `claim` and `policy`, where recruitment has one.
- **Caps are ordered by severity**: an exclusion (score 0) outranks a breached condition precedent (25),
  which outranks no insuring clause responding (40). First match wins, so the rubric states its decisive
  rule first.
- **`conditional` is worth 0.5**, which recruitment has no equivalent of. A clause that responds subject to
  conditions is genuinely half an answer.

## 5. What this does *not* yet prove

Honest limits, because an acceptance test that overclaims is worse than none:

- **The engine is proven; the journeys are not.** No insurance grader adapter exists, so J1/J2/J3 have not
  been run end to end on a real claim. What is proven is that the scoring and grounding capabilities are
  domain-neutral — which is the part that would have forced a fork.
- The pack has not been published through registry-service, so runtime pack resolution for a second domain
  is untested.
- The prompts and ontology are written but unexercised: no LLM has been asked to extract a policy wording.
- No insurance `Subject` rows, so nothing has been stored under `subject_type: claim`.

## 6. Next for this domain

1. An insurance grader adapter — the sibling of `ats-orchestrator/workflows/bulk_adapters.py`, and the one
   file a new domain genuinely has to write.
2. Publish the pack through registry-service and resolve it at runtime.
3. Run J1 on a policy document and J2 on a claim against it.
