"""The deterministic scoring engine: graded criteria + a rubric -> a score, a band and a recommendation.

No model is involved and none ever may be (CLAUDE.md section 2 forbids LLM arithmetic). An LLM may decide
that a criterion is `moderate`; what `moderate` is worth, and what the total means, is decided here from the
pack's rubric. That separation is the whole reason a regulated domain can use this platform: the judgement is
reviewable, the arithmetic is reproducible, and both are versioned.

Everything is pure: same inputs, same output, no clock, no I/O, no global state. A run can therefore be
re-scored later from stored grades to answer "why did this come out at 61?" without re-running anything.
"""
from dataclasses import dataclass, field
from typing import Optional

from agent_common.rules.models import (Cap, Component, Deduction, GradedComponent, InputComponent,
                                       RubricDocument, Selector)


@dataclass(frozen=True)
class CriterionOutcome:
    """One graded criterion, as the engine needs it: a name, a grade, and attributes to select on.

    `attributes` is how the domain speaks to the rubric without the engine understanding it: recruitment
    passes `{"importance": "must", "core": "true"}`, insurance `{"clause_type": "exclusion"}`. Values are
    compared as lower-case strings.
    """

    name: str
    grade: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreOutcome:
    """The result, with everything needed to explain it.

    `components` and `deductions` hold unrounded points so a caller can present them however it likes;
    `score` is the final, clamped, rounded number that a person sees and a threshold is checked against.
    """

    score: float
    band: str
    recommendation: str
    components: dict[str, float]
    deductions: dict[str, float]
    total_deductions: float
    cap: Optional[float] = None
    cap_reason: str = ""


def _matches(outcome: CriterionOutcome, selector: Selector) -> bool:
    """Does this criterion carry every attribute the selector asks for?"""
    if selector.matches_everything:
        return True
    for name, allowed in selector.attributes.items():
        value = str(outcome.attributes.get(name, "")).strip().lower()
        if value not in {str(option).strip().lower() for option in allowed}:
            return False
    return True


def _select(criteria: list[CriterionOutcome], selector: Selector) -> list[CriterionOutcome]:
    return [outcome for outcome in criteria if _matches(outcome, selector)]


def _mean_weight(rubric: RubricDocument, selected: list[CriterionOutcome]) -> float:
    if not selected:
        return 0.0
    return sum(rubric.grade_weight(outcome.grade) for outcome in selected) / len(selected)


def _graded_points(rubric: RubricDocument, component: GradedComponent, criteria: list[CriterionOutcome],
                   resolved: dict[str, list[CriterionOutcome]]) -> tuple[float, list[CriterionOutcome]]:
    """Points for a graded component, after resolving its fallback.

    The fallback matters more than it looks: without it, a source that names only optional criteria would
    score zero on the component that carries most of the weight, which reads as "unsuitable" when the truth
    is "the source did not say".
    """
    selected = _select(criteria, component.select)
    if not selected and component.fallback_component:
        selected = resolved.get(component.fallback_component, [])
    if not selected and component.fallback_to_all:
        selected = list(criteria)
    return component.points * _mean_weight(rubric, selected), selected


def _input_points(component: InputComponent, inputs: dict[str, Optional[float]]) -> float:
    """Points for an input component: thresholds on the primary value, or the fallback value clamped.

    The fallback deliberately ignores the thresholds. Thresholds describe one measurement (a ratio against a
    stated requirement); when that measurement does not exist, the fallback is a different measurement on the
    points scale itself, and reading it through the first one's thresholds would be nonsense.
    """
    value = inputs.get(component.input)
    if value is not None:
        if not component.thresholds:
            return min(max(float(value), 0.0), component.points)
        for threshold in component.thresholds:       # validated: highest min first
            if float(value) >= threshold.min:
                return threshold.points
        return 0.0
    if component.fallback_input is not None:
        fallback = inputs.get(component.fallback_input)
        if fallback is not None:
            return min(max(float(fallback), 0.0), component.points)
    return 0.0


def _deduction_applies(rubric: RubricDocument, deduction: Deduction, criteria: list[CriterionOutcome],
                       flags: dict[str, bool]) -> bool:
    if deduction.flag is not None:
        return bool(flags.get(deduction.flag))
    among = [outcome for outcome in _select(criteria, deduction.select) if outcome.grade in deduction.among]
    if not among:
        return False
    return sum(outcome.grade == deduction.majority_grade for outcome in among) > len(among) / 2


def _cap_applies(cap: Cap, criteria: list[CriterionOutcome]) -> tuple[bool, list[str]]:
    """Whether a cap fires, and the criteria that made it fire (for the reason shown to a person)."""
    selected = _select(criteria, cap.select)
    if not selected:
        return False, []
    hit = [outcome for outcome in selected if outcome.grade in cap.grades]
    if not hit or len(hit) < cap.min_fraction * len(selected):
        return False, []
    return True, [outcome.name for outcome in hit]


def _band_for(rubric: RubricDocument, score: float) -> tuple[str, str]:
    for band in rubric.bands:                        # validated: highest min first
        if score >= band.min:
            return band.band, band.recommendation
    last = rubric.bands[-1]
    return last.band, last.recommendation


def score(rubric: RubricDocument, criteria: list[CriterionOutcome], *,
          inputs: Optional[dict[str, Optional[float]]] = None,
          flags: Optional[dict[str, bool]] = None) -> ScoreOutcome:
    """Apply a rubric to graded criteria.

    `inputs` are the numbers the rubric's input components name (a ratio, a fitness judgement); a missing or
    `None` input simply means the component falls back or scores nothing. `flags` are named booleans the
    deductions test. Neither may change what a grade is worth - only the rubric does that.

    Returns the score clamped to the rubric's range and rounded to one decimal, with the component points,
    the deductions, any cap that fired and why, plus the band and recommendation. Raises nothing: an unknown
    grade is worth 0, an unknown input is absent, and an unknown flag is false, because a rubric must not be
    able to fail a run that has already done its real work.
    """
    inputs, flags = dict(inputs or {}), dict(flags or {})

    if not criteria:
        # Nothing could be graded. Everything scores zero and the reason says so, rather than reporting a
        # confident number built entirely from inputs the criteria were supposed to qualify.
        band, recommendation = _band_for(rubric, 0.0)
        return ScoreOutcome(score=0.0, band=band, recommendation=recommendation,
                            components={component.key: 0.0 for component in rubric.components},
                            deductions={}, total_deductions=0.0, cap=0.0,
                            cap_reason=rubric.empty_criteria_reason)

    points: dict[str, float] = {}
    resolved: dict[str, list[CriterionOutcome]] = {}
    for component in rubric.components:
        if isinstance(component, GradedComponent):
            earned, selected = _graded_points(rubric, component, criteria, resolved)
            resolved[component.key] = selected
        else:
            earned = _input_points(component, inputs)
        points[component.key] = earned

    taken: dict[str, float] = {}
    for deduction in rubric.deductions:
        if _deduction_applies(rubric, deduction, criteria, flags):
            taken[deduction.key] = deduction.points
    total_deductions = sum(taken.values())

    subtotal = sum(points.values()) - total_deductions

    cap_value: Optional[float] = None
    cap_reason = ""
    for cap in rubric.caps:                          # ordered: the first that applies wins
        applies, names = _cap_applies(cap, criteria)
        if applies:
            cap_value = cap.max_score
            cap_reason = cap.reason.replace("{names}", ", ".join(names))
            subtotal = min(subtotal, cap.max_score)
            break

    final = round(min(max(subtotal, 0.0), rubric.max_score), 1)
    band, recommendation = _band_for(rubric, final)
    return ScoreOutcome(score=final, band=band, recommendation=recommendation, components=points,
                        deductions=taken, total_deductions=total_deductions, cap=cap_value,
                        cap_reason=cap_reason)


def check_thresholds(rubric: RubricDocument, outcome: ScoreOutcome) -> dict[str, object]:
    """The band's verdict as a small, JSON-safe record for a decision or an approval step.

    Separate from `score` because a caller often needs the verdict without the arithmetic - a J6 review
    screen, an audit export - and because what counts as "needs review" is a band property, not a number
    anybody should re-derive.
    """
    return {"score": outcome.score, "band": outcome.band, "recommendation": outcome.recommendation,
            "capped": outcome.cap is not None, "capReason": outcome.cap_reason,
            "rubric": rubric.key, "rubricVersion": rubric.version}
