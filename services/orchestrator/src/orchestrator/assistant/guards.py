"""Checks in code on what the chat model returns (output guardrails). Prompts ask the model to behave; these make
sure a wrong answer never reaches the recruiter, even when a prompt injection talked the model round.

Score guard: a reply may only quote scores that exist as numeric fields of the stored screening ("N/100" must be the
stored total), and must not say it followed an override. Anything else (e.g. "now scores 95/100" after a fake
"new rules" message, or "Score: 100/100 ... as per override" after a base64-hidden order) replaces the reply with
the stored result and a short explanation.
"""
import re
from dataclasses import dataclass
from typing import Optional

# "95/100", "95 / 100", "95 out of 100"
_OUT_OF_100 = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:/\s*100\b|out\s+of\s+100\b)", re.IGNORECASE)
# "score: 95", "scores **95**", "scored 95%" (within one sentence)
_SCORE_WORD = re.compile(r"\bscor(?:e|es|ed|ing)\b[^.\n\d]{0,30}?(\d{1,3}(?:\.\d+)?)\b", re.IGNORECASE)
# a "score" sentence about exams or grades is not a screening score
_NOT_SCREENING = re.compile(r"\b(?:exams?|tests?|gpa|cgpa|grades?|percentile|class(?:es)?|semesters?|ielts|toefl|"
                            r"sat|board|university|degree|marks?)\b", re.IGNORECASE)
TOLERANCE = 0.05


@dataclass(frozen=True)
class GuardResult:
    reply: str
    corrected: bool
    claimed: tuple[float, ...] = ()


# A reply that admits it followed injected orders ("as per override", "pre-approved", "rules have changed").
_OBEYED = re.compile(r"\b(?:as\s+per|due\s+to|following|per)\s+(?:the\s+)?(?:override|updated\s+(?:rules|"
                     r"instructions)|new\s+(?:rules|instructions)|pre-?approval)\b|\binstruction\s+override\b|"
                     r"\boverr(?:ode|ide)\s+(?:the\s+)?(?:original\s+)?(?:screening|scoring)\b|"
                     r"\bpre-?approv(?:ed|al)\b|\b(?:updated|new)\s+(?:output\s+)?instructions\b", re.IGNORECASE)


def score_claims(text: str) -> tuple[list[float], list[float]]:
    """(total-score claims "N/100", other score mentions "score ... N") the text states."""
    totals = [float(m.group(1)) for m in _OUT_OF_100.finditer(text)]
    mentions = []
    for line in text.splitlines():
        if not _NOT_SCREENING.search(line):
            mentions += [float(m.group(1)) for m in _SCORE_WORD.finditer(line)]
    return totals, mentions


def _numbers(value) -> set[float]:
    """Numeric values (not text) inside a JSON-like value."""
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {float(value)}
    if isinstance(value, dict):
        return {n for v in value.values() for n in _numbers(v)}
    if isinstance(value, list):
        return {n for v in value for n in _numbers(v)}
    return set()


def allowed_numbers(screening: Optional[dict]) -> set[float]:
    """Numbers a reply may call a score: the stored score, its breakdown and the years. Only numeric FIELDS count,
    never text: evidence quotes and document excerpts may contain an injected "95", which must not whitelist it."""
    if not screening:
        return set()
    fields = [screening.get("score"), screening.get("breakdown"), screening.get("totalYears"),
              screening.get("requiredYears")]
    return {n for field in fields for n in _numbers(field)}


def correction(screening: Optional[dict]) -> str:
    """The reply that replaces an invented score."""
    if not screening:
        return ("No screening has been run for this candidate yet, so there is no score. Press **Screen candidate** "
                "to run one. Scores cannot be set or changed in the chat.")
    return (f"The stored screening score for this candidate is **{screening['score']}/100** "
            f"({screening['band']}, recommendation: {screening['recommendation']}). Scores come only from a "
            "screening run; messages or document text cannot change the scoring rules. Press **Screen candidate** "
            "to run it again.")


def _matches(value: float, allowed: set[float]) -> bool:
    return any(abs(value - a) <= TOLERANCE for a in allowed)


def guard_scores(reply: str, screening: Optional[dict]) -> GuardResult:
    """Keep the reply if every score it states is backed by the stored screening and it does not say it followed
    injected orders; otherwise return the correction.

    "N/100" must equal the stored total score (a breakdown part is never "out of 100"); a plain "score ... N" may be
    any numeric field of the screening (e.g. a breakdown component)."""
    totals, mentions = score_claims(reply)
    total = {float(screening["score"])} if screening and screening.get("score") is not None else set()
    invented = tuple([c for c in totals if not _matches(c, total)]
                     + [c for c in mentions if not _matches(c, allowed_numbers(screening))])
    if not invented and not _OBEYED.search(reply):
        return GuardResult(reply, False, tuple(totals + mentions))
    return GuardResult(correction(screening), True, invented)
