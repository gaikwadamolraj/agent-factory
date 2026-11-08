# agent-match and agent-rules — the shared matching engine

Stage E of docs/impl_multi_domain.md: the part of "grade a subject against a set of criteria" that is the
same in recruitment, insurance, banking and media, separated from the part that is not. Built 8 November
2026.

## 1. Where the line is

**In `agent_common` (every industry):**

| Package | What it owns |
|---|---|
| `agent_common.match.text` | Is this term, phrase or name actually written in the source? Whole-term matching that survives `.NET`, `C++`, `C#`, `Node.js`; key-word overlap for honest rewording; snippets quoted from the stored source. |
| `agent_common.match.grounding` | `verify_criteria`, `verify_attributes`, `verify_grades`, `prematch` — over neutral `Criterion` / `Attribute` / `GradeClaim` dataclasses, with a `GroundingPolicy` carrying the domain's grade names and correction wording. |
| `agent_common.rules` | `RubricDocument` and `score()` — weighted components, deductions, caps, bands, recommendations. Pure arithmetic over grades. |

**In the industry's own service (agent-skill, for recruitment):**

- reading `2019-03`, `2021` and `present`, and merging overlapping employment into years;
- the JD/resume vocabulary and the mapping to and from the neutral dataclasses;
- which attributes the rubric selects on (`importance`, `core`, `kind`);
- the words a recruiter reads in a correction (`no real candidate skill cited`);
- the packaged default rubric, `screening/rubric.py`.

The rule that decided it: *if it is only true of one industry, it stays with that industry.* A grounding fix
is then one fix for every regulated domain, and recruitment's language never reaches the shared code.

## 2. Why libraries and not two more MCP services

Neither has its own data or its own runtime — they are functions over values the caller already holds. A
service boundary would mean shipping resume text over the wire again, a second LLM session, two more OAuth
clients, network policies and secrets, in exchange for nothing. CLAUDE.md's service-vs-lib rule says the
same: reused code goes to `services/common`. If a domain ever needs match as a standalone service, the
library is what that service would wrap.

## 3. The rubric is data

What used to be Python in `screening/scoring.py` is now `packs/ats/v1/rubrics/fit.rubric.json`:

- **components** — must-haves 50, nice-to-haves 10, experience 15, responsibilities 15, education 10;
- **fallbacks** — no must-haves stated means every requirement counts; no nice-to-haves means the must-have
  result stands in, so a silent JD neither rewards nor punishes a candidate;
- **deductions** — 15 for a resume that tries to instruct the screening system, 5 when most positive grades
  are merely asserted;
- **caps** — 40 when half the must-haves are absent, 55 for a missing core requirement, read in order so the
  broader failure outranks the narrower one;
- **bands** — 90 / 75 / 60 / 40 with their recommendations.

A client whose activated skill version ships `rubrics/fit.rubric.json` is scored by *that* file; otherwise
the service's packaged default applies, so versions authored before rubrics existed are unchanged. A rubric
that will not parse is refused when the version loads, and the packaged one is used — scoring a candidate
with a rubric nobody could read would be worse than scoring them with the one the service shipped.

**The grade vocabulary is rubric data.** Insurance writes `covered / excluded / conditional /
insufficient_evidence` with their own weights and its own caps ("any exclusion is decisive" is a cap with
`minFraction: 0`), and no Python changes.

### What the engine refuses

A rubric is authored outside the code review that protects the platform, so the model is closed and
validated: components must sum to the maximum score, bands must descend and reach 0, thresholds must
descend, caps and deductions may only name grades the rubric defines, and fallbacks may not point at
unknown components or at themselves. An unknown grade at run time is worth 0 rather than raising — a model
inventing a word must not be able to fail a run, nor earn credit for it.

## 4. What was verified

Behaviour had to be identical, and "identical" was pinned before anything moved.

| Check | Result |
|---|---|
| 60 scoring cases (every grade, both caps, both deductions, the experience ladder, clamping, empty input) against the hard-coded engine | **0 differ** |
| The same 60 against the rubric file published in `packs/ats/v1` | **0 differ** |
| 1,201 grounding comparisons (text predicates, `verify_jd`, `verify_profile`, `verify_matches`, including adversarial resumes, empty sources and a skipped requirement) | **0 differ** |
| Service suites | 211 passed |

The grounding comparison initially showed 465 differences, all of them in the wording of the corrections a
recruiter reads ("no real candidate skill cited" → "no real evidence cited"). That is domain vocabulary, so
the templates moved into `GroundingPolicy` and recruitment kept its own words — which is the same rule that
decided everything else here.

## 5. Open

- `steps.py` still orchestrates in ATS terms (`analyze_job_description`, `match_requirements`). Turning the
  four steps into pack-driven `extract_criteria` / `extract_subject` / `grade_criteria` / `render_report` is
  the rest of Stage E, and the insurance pack (step 6) is what will force it.
- `render_report(results, template)` does not exist yet: the verdict prompt is still ATS-shaped.
- No report template in the pack (`reports: {}`).
