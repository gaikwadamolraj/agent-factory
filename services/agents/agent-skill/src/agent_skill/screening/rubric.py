"""The recruitment scoring rubric: ATS data for the shared `agent_common.rules` engine.

This is the industry half of scoring and it stays in this service. The arithmetic - weighted components,
deductions, caps, bands - is identical for every domain and lives in `agent_common.rules`; what a job
requirement is worth is recruitment's business and lives here, as data.

It is a packaged default, not the source of truth at run time: a client whose activated skill version ships
`rubrics/fit.rubric.json` is scored by that file instead, so a rubric change is a pack publication rather
than a release of this service. `scripts/build_ats_pack.py` reads this module when it builds the ATS pack,
so the two cannot drift.

The numbers are the ones this service has always used. They are stated here rather than in Python
expressions so that a reviewer can read the policy without reading an algorithm.
"""
from functools import lru_cache

from agent_common.rules import RubricDocument

# Deliberately plain data: no computation, no imports from the scoring code, nothing an LLM is shown.
FIT_RUBRIC: dict = {
    "key": "fit",
    "version": "1",
    "displayName": "Candidate fit",
    "maxScore": 100,
    "grades": {
        "strong": {"weight": 1.0, "label": "Strong"},
        "moderate": {"weight": 0.6, "label": "Moderate"},
        "adjacent": {"weight": 0.4, "label": "Adjacent"},
        "claimed": {"weight": 0.25, "label": "Claimed only"},
        "invalidated": {"weight": 0.0, "label": "Invalidated"},
        "absent": {"weight": 0.0, "label": "Absent"},
    },
    "positiveGrades": ["strong", "moderate", "adjacent", "claimed"],
    "components": [
        # Must-haves carry half the score. With no must-haves stated, every requirement counts instead, so a
        # JD that marks nothing mandatory is still scored on what it does ask for.
        {"kind": "graded", "key": "must_have", "points": 50,
         "select": {"attributes": {"importance": ["must"]}}, "fallbackToAll": True},
        # Nice-to-haves are worth little, and when a JD states none the must-have result stands in, so their
        # absence neither rewards nor punishes a candidate.
        {"kind": "graded", "key": "nice_to_have", "points": 10,
         "select": {"attributes": {"importance": ["nice"]}}, "fallbackComponent": "must_have"},
        # Experience against the years the JD asks for. Without a stated minimum there is no ratio to
        # compute, and the model's seniority judgement is used on the same 0-15 scale.
        {"kind": "input", "key": "experience", "points": 15, "input": "experience_ratio",
         "thresholds": [{"min": 1.0, "points": 15}, {"min": 0.75, "points": 10},
                        {"min": 0.5, "points": 5}, {"min": 0.0, "points": 0}],
         "fallbackInput": "seniority_fit"},
        {"kind": "input", "key": "responsibilities", "points": 15, "input": "responsibility_fit"},
        {"kind": "input", "key": "education", "points": 10, "input": "education_fit"},
    ],
    "deductions": [
        # A resume that tries to instruct the screening system costs points and is reported. The grades are
        # untouched by it: they still had to quote the resume to be awarded.
        {"key": "manipulation", "points": 15, "flag": "manipulation"},
        # Mostly asserted rather than evidenced: a small, deliberate penalty for a resume that lists
        # everything and shows nothing.
        {"key": "mostly_claimed", "points": 5, "majorityGrade": "claimed",
         "among": ["strong", "moderate", "claimed"]},
    ],
    "caps": [
        # Read in order, first match wins - so the broader failure outranks the single missing core skill.
        {"key": "half_musts_missing", "maxScore": 40, "grades": ["absent", "invalidated"],
         "minFraction": 0.5, "select": {"attributes": {"importance": ["must"]}},
         "reason": "half or more of the must-have requirements are absent"},
        {"key": "core_missing", "maxScore": 55, "grades": ["absent", "invalidated"],
         "select": {"attributes": {"core": ["true"]}},
         "reason": "core requirement missing: {names}"},
    ],
    "bands": [
        {"min": 90, "band": "Exceptional", "recommendation": "Shortlist"},
        {"min": 75, "band": "Strong", "recommendation": "Shortlist"},
        {"min": 60, "band": "Partial", "recommendation": "Interview to verify gaps"},
        {"min": 40, "band": "Weak", "recommendation": "Do not proceed"},
        {"min": 0, "band": "Not suited", "recommendation": "Do not proceed"},
    ],
    "emptyCriteriaReason": "No requirements could be read from the JD.",
}


@lru_cache(maxsize=1)
def default_rubric() -> RubricDocument:
    """The packaged rubric, validated once. Used when the skill version carries no rubric file."""
    return RubricDocument.model_validate(FIT_RUBRIC)
