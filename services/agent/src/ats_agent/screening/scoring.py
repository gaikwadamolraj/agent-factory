"""Deterministic years-of-experience and rubric scoring (no LLM arithmetic)."""
import re
from datetime import date

from ats_agent.schemas import CandidateProfile, JDAnalysis, RequirementMatch, ScoreBreakdown

GRADE_WEIGHT = {"strong": 1.0, "moderate": 0.6, "adjacent": 0.4, "claimed": 0.25, "invalidated": 0.0, "absent": 0.0}
GRADE_LABEL = {"strong": "Strong", "moderate": "Moderate", "adjacent": "Adjacent", "claimed": "Claimed only",
               "invalidated": "Invalidated", "absent": "Absent"}
_ONGOING = {"present", "current", "now", "today", "till date", "ongoing", "to date"}


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
    return _merged_years(_intervals(profile))


def skill_years(profile: CandidateProfile, skill_names: list[str]) -> float | None:
    return _merged_years(_intervals(profile, skill_names))


def band(score: float) -> str:
    return ("Exceptional" if score >= 90 else "Strong" if score >= 75 else "Partial" if score >= 60
            else "Weak" if score >= 40 else "Not suited")


def recommendation(score: float) -> str:
    return "Shortlist" if score >= 75 else "Interview to verify gaps" if score >= 60 else "Do not proceed"


def compute_score(
    analysis: JDAnalysis,
    matches: list[RequirementMatch],
    years: float | None,
    seniority_fit: int,
    responsibility_fit: int,
    education_fit: int,
    manipulation: bool,
) -> tuple[float, ScoreBreakdown]:
    """Rubric: must-haves 50, nice-to-haves 10, experience 15, responsibilities 15, education 10; then deductions/caps."""
    reqs = analysis.requirements
    if not reqs:
        return 0.0, ScoreBreakdown(must_have=0, nice_to_have=0, experience=0, responsibilities=0, education=0,
                                   deductions=0, cap=0, cap_reason="No requirements could be read from the JD.")
    weights = [GRADE_WEIGHT[m.grade] for m in matches]
    musts = [w for r, w in zip(reqs, weights) if r.importance == "must"] or weights
    nices = [w for r, w in zip(reqs, weights) if r.importance == "nice"]

    must = 50 * sum(musts) / len(musts)
    nice = 10 * (sum(nices) / len(nices) if nices else sum(musts) / len(musts))
    if analysis.min_total_years and years is not None:
        ratio = years / analysis.min_total_years
        experience = 15 if ratio >= 1 else 10 if ratio >= 0.75 else 5 if ratio >= 0.5 else 0
    else:
        experience = min(max(seniority_fit, 0), 15)
    responsibilities = min(max(responsibility_fit, 0), 15)
    education = min(max(education_fit, 0), 10)

    deductions = 15.0 if manipulation else 0.0
    positive = [m for m in matches if m.grade in ("strong", "moderate", "claimed")]
    if positive and sum(m.grade == "claimed" for m in positive) > len(positive) / 2:
        deductions += 5

    score = must + nice + experience + responsibilities + education - deductions
    cap, cap_reason = None, ""
    must_matches = [m for r, m in zip(reqs, matches) if r.importance == "must"]
    if must_matches and sum(m.grade in ("absent", "invalidated") for m in must_matches) >= len(must_matches) / 2:
        cap, cap_reason = 40.0, "half or more of the must-have requirements are absent"
    core_missing = [r.name for r, m in zip(reqs, matches) if r.core and m.grade in ("absent", "invalidated")]
    if core_missing and cap is None:
        cap, cap_reason = 55.0, f"core requirement missing: {', '.join(core_missing)}"
    if cap is not None:
        score = min(score, cap)

    score = round(min(max(score, 0.0), 100.0), 1)
    return score, ScoreBreakdown(
        must_have=round(must, 1), nice_to_have=round(nice, 1), experience=experience,
        responsibilities=responsibilities, education=education, deductions=deductions, cap=cap, cap_reason=cap_reason,
    )
