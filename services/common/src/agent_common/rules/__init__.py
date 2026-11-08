"""`agent-rules`: deterministic scoring from a pack-supplied rubric (docs/impl_multi_domain.md section 2.2).

This is the shared half of grading - the arithmetic every domain does identically. The domain half (what a
job requirement is, how years of experience are counted, what a coverage clause means) stays in that
domain's own service, which is what keeps this module free of any industry's vocabulary.

    rubric = parse_rubric(path.read_text())
    outcome = score(rubric, criteria, inputs={"experience_ratio": 1.2}, flags={"manipulation": False})

A rubric is data, authored in a pack and validated on publish, so `parse_rubric` raises `AppError` with the
first problem named rather than letting a malformed rubric reach a user's request.
"""
import json
from typing import Any

from agent_common.errors import AppError, Errors
from agent_common.rules.models import (Band, Cap, Component, Deduction, GradeSpec, GradedComponent,
                                       InputComponent, RubricDocument, Selector, Threshold)
from agent_common.rules.scoring import CriterionOutcome, ScoreOutcome, check_thresholds, score


def parse_rubric(text: str) -> RubricDocument:
    """Parse and validate a rubric file. Raises `AppError(VAL-INV-0001)` naming the first problem."""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as error:
        raise AppError(Errors.VALIDATION_FAILED,
                       fields=[{"field": "rubric", "message": f"not valid JSON: {error}"}]) from None
    if not isinstance(data, dict):
        raise AppError(Errors.VALIDATION_FAILED,
                       fields=[{"field": "rubric", "message": "a rubric file must be a JSON object"}])
    try:
        return RubricDocument.model_validate(data)
    except Exception as error:
        raise AppError(Errors.VALIDATION_FAILED,
                       fields=[{"field": "rubric", "message": _first_problem(error)}]) from None


def _first_problem(error: Exception) -> str:
    """The first validation problem as one short sentence, never a stack trace (CLAUDE.md section 5)."""
    errors = getattr(error, "errors", None)
    if callable(errors):
        for item in errors():
            location = ".".join(str(part) for part in item.get("loc", ())) or "rubric"
            return f"{location}: {item.get('msg', 'invalid value')}"
    return str(error)[:200]


__all__ = ["Band", "Cap", "Component", "CriterionOutcome", "Deduction", "GradeSpec", "GradedComponent",
           "InputComponent", "RubricDocument", "ScoreOutcome", "Selector", "Threshold", "check_thresholds",
           "parse_rubric", "score"]
