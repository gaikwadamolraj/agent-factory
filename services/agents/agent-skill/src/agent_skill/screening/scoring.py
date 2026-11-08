"""Recruitment's half of scoring: dates, years of experience, and the shape of a graded requirement.

The arithmetic that turns grades into a score, a band and a recommendation is *not* here - it is
`agent_common.rules`, shared with every other domain, driven by the rubric in `rubric.py` (or the one the
client's activated pack supplies). What stays here is what only recruitment knows:

* how to read "2019-03", "2021", "present" and merge overlapping employment into a number of years;
* which attributes of a requirement the rubric selects on (`importance`, `core`);
* which numbers feed the rubric's input components.

No LLM arithmetic anywhere in this path: a model grades a requirement, this module measures time, and the
rules engine does the sums (CLAUDE.md section 2).
"""
import re
from datetime import date
from typing import Optional

from agent_common.rules import CriterionOutcome, RubricDocument, ScoreOutcome, score

from agent_skill.schemas import CandidateProfile, JDAnalysis, RequirementMatch, ScoreBreakdown
from agent_skill.screening.rubric import default_rubric

_ONGOING = {"present", "current", "now", "today", "till date", "ongoing", "to date"}


def rubric_for(bundle) -> RubricDocument:
    """The rubric to score with: the skill version's own, or this service's packaged default.

    A client that has published `rubrics/fit.rubric.json` on its activated version is scored by that file,
    which is what makes re-weighting a rubric a pack publication instead of a release of this service.
    """
    return getattr(bundle, "rubric", None) or default_rubric()


# ------------------------------------------------------------------ dates
def _parse(value: str | None, *, is_end: bool) -> date | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in _ONGOING:
        return date.today()
    m = re.search(r"(\d{4})(?:[-/.](\d{1,2}))?", v)
    if not m:
        return None
    year, month = int(m.group(1)), min(max(int(m.group(2) or (12 if is_end else 1)), 1), 12)
    if is_end:  # an end month counts in full: "2020-12" means up to 1 Jan 2021
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return date(year, month, 1)


def _merged_years(intervals: list[tuple[date, date]]) -> float | None:
    """Total years covered by the intervals, counting overlapping employment once."""
    if not intervals:
        return None
    intervals.sort()
    days, (cur_s, cur_e) = 0, intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            days += (cur_e - cur_s).days
            cur_s, cur_e = s, e
    days += (cur_e - cur_s).days
    return round(days / 365.25, 1)


def _intervals(profile: CandidateProfile, skill_names: list[str] | None = None) -> list[tuple[date, date]]:
    wanted = {n.lower() for n in skill_names} if skill_names is not None else None
    out = []
    for role in profile.roles:
        if wanted is not None and not wanted & {s.lower() for s in role.skills_used}:
            continue
        start, end = _parse(role.start, is_end=False), _parse(role.end, is_end=True) or date.today()
        if start and end >= start:
            out.append((start, end))
    return out


def total_years(profile: CandidateProfile) -> float | None:
    """Years of professional experience, overlapping roles counted once."""
    return _merged_years(_intervals(profile))


def skill_years(profile: CandidateProfile, skill_names: list[str]) -> float | None:
    """Years in the roles where at least one of these skills was used."""
    return _merged_years(_intervals(profile, skill_names))


# ---------------------------------------------------------------- scoring
def criterion_outcomes(analysis: JDAnalysis, matches: list[RequirementMatch]) -> list[CriterionOutcome]:
    """Graded requirements in the shape the rules engine selects on.

    `importance` and `core` become plain attributes: the engine matches them without knowing that one means
    "the JD insists" and the other "central to the job title". That is what lets insurance pass
    `clause_type` through the same code.
    """
    return [CriterionOutcome(name=requirement.name, grade=match.grade,
                             attributes={"importance": requirement.importance,
                                         "core": str(requirement.core).lower(),
                                         "kind": requirement.kind})
            for requirement, match in zip(analysis.requirements, matches)]


def score_fit(rubric: RubricDocument, analysis: JDAnalysis, matches: list[RequirementMatch],
              years: Optional[float], seniority_fit: int, responsibility_fit: int, education_fit: int,
              manipulation: bool) -> ScoreOutcome:
    """Score a screening: build the rubric's inputs from the analysis, then let the rules engine decide.

    `experience_ratio` is `None` unless the JD states a minimum *and* the resume's dates could be read; the
    rubric then falls back to the model's seniority judgement. That distinction matters - scoring a ratio
    against an unstated requirement would invent a number nobody asked for.
    """
    ratio = (years / analysis.min_total_years) if (analysis.min_total_years and years is not None) else None
    return score(rubric, criterion_outcomes(analysis, matches),
                 inputs={"experience_ratio": ratio, "seniority_fit": float(seniority_fit),
                         "responsibility_fit": float(responsibility_fit),
                         "education_fit": float(education_fit)},
                 flags={"manipulation": manipulation})


def breakdown_of(outcome: ScoreOutcome) -> ScoreBreakdown:
    """The score outcome in the shape the API and the UI already expect.

    The component keys are the recruitment rubric's, so this mapping is recruitment's job: another domain's
    report reads `outcome.components` under its own names.
    """
    points = outcome.components
    return ScoreBreakdown(
        must_have=round(points.get("must_have", 0.0), 1),
        nice_to_have=round(points.get("nice_to_have", 0.0), 1),
        experience=points.get("experience", 0.0),
        responsibilities=points.get("responsibilities", 0.0),
        education=points.get("education", 0.0),
        deductions=outcome.total_deductions,
        cap=outcome.cap,
        cap_reason=outcome.cap_reason,
    )
