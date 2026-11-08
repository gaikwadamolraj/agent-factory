"""Grounding: every criterion, every attribute and every grade must trace back to a stored source.

This is the `agent-match` capability's deterministic half (docs/impl_multi_domain.md section 2.2). A model
proposes; this disposes. Three checks, in the order a match runs:

1. `verify_criteria` - keep only the criteria the criteria source actually names.
2. `verify_attributes` - keep only the subject attributes the subject source actually states, and recover
   the ones the model missed but the source spells out.
3. `verify_grades` - a positive grade must cite a real attribute; one that cites nothing is demoted.

None of it knows what a job requirement, a coverage clause or a KYC control is. It knows that a claim about
a document must be findable in that document, which is the same obligation in every regulated domain - and
the reason a grounding fix here fixes every domain at once.
"""
from dataclasses import dataclass, field, replace
from typing import Optional

from agent_common.match.text import (DEFAULT_OVERLAP, DEFAULT_STOPWORDS, normalize, phrase_in_text, same,
                                     snippet, supported_by, term_in_text)


@dataclass
class Criterion:
    """One thing a subject is graded against, as extracted from the criteria source."""

    name: str
    quote: str = ""                                  # verbatim phrase from the source that introduced it
    related_terms: list[str] = field(default_factory=list)


@dataclass
class Attribute:
    """One thing the subject source states about the subject, with the words that show it."""

    name: str
    evidence: str = ""
    source: str = ""


@dataclass
class GradeClaim:
    """A model's claim about one criterion: the grade and the attributes it says support it."""

    criterion_index: int
    grade: str
    cited: list[str] = field(default_factory=list)
    evidence: str = ""


@dataclass(frozen=True)
class GroundingPolicy:
    """What the caller's domain considers a positive claim, and what an ungrounded one becomes.

    `demoted_grade` is where a claim with no evidence lands, and `asserted_grade` where one the source
    merely lists lands. Both are grade names from the pack's own vocabulary, so insurance demotes to
    `insufficient_evidence` without any change here.
    """

    positive_grades: frozenset[str]
    demoted_grade: str
    asserted_grade: str
    max_related_terms: int = 8
    stopwords: frozenset[str] = DEFAULT_STOPWORDS
    overlap: float = DEFAULT_OVERLAP
    # What a correction says to the person who reads the run. The wording is the domain's - a recruiter
    # should read "no real candidate skill cited", an underwriter something about evidence in the claim file
    # - so the templates come from the caller. Placeholders: {criterion}, {from}, {to}, {names}.
    demotion_note: str = "{criterion}: {from} -> {to} (no real evidence cited)"
    assertion_note: str = "{criterion}: {from} -> {to} (the source names {names})"

    def demotion(self, criterion: str, grade: str) -> str:
        return self.demotion_note.format(criterion=criterion, **{"from": grade, "to": self.demoted_grade},
                                         names="")

    def assertion(self, criterion: str, names: list[str]) -> str:
        return self.assertion_note.format(criterion=criterion,
                                          **{"from": self.demoted_grade, "to": self.asserted_grade},
                                          names=", ".join(names))


def verify_criteria(criteria: list[Criterion], source_text: str,
                    policy: GroundingPolicy) -> tuple[list[Criterion], list[str]]:
    """Keep the criteria the source really names, de-duplicated. Returns (kept, dropped names).

    A criterion the source does not contain is not a strict requirement - it is one the model invented, and
    grading a subject against it would produce a confident number about a question nobody asked.
    """
    kept: list[Criterion] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for criterion in criteria:
        key = normalize(criterion.name)
        grounded = supported_by(criterion.name, criterion.quote, source_text,
                                stopwords=policy.stopwords, overlap=policy.overlap)
        if not grounded or not key or key in seen:
            dropped.append(criterion.name)
            continue
        quote = criterion.quote
        if not phrase_in_text(quote, source_text):
            # The model's quotation is not in the source; fall back to the name, which is, so a person
            # checking the criterion is never shown a quotation the document does not contain.
            quote = criterion.name
        related = [term for term in dict.fromkeys(criterion.related_terms)
                   if term.strip() and not same(term, criterion.name)][: policy.max_related_terms]
        seen.add(key)
        kept.append(replace(criterion, quote=quote, related_terms=related))
    return kept, dropped


def verify_attributes(attributes: list[Attribute], source_text: str, policy: GroundingPolicy,
                      criteria: Optional[list[Criterion]] = None) -> tuple[list[Attribute], list[str]]:
    """Keep attributes the source states, and recover criteria terms written in it but missed.

    The recall half matters as much as the precision half: a model that overlooks a term the source spells
    out would have the subject marked down for something it plainly has.
    """
    kept: list[Attribute] = []
    dropped: list[str] = []
    for attribute in attributes:
        # Stricter than `verify_criteria` on purpose: a criterion may be reworded by the source that states
        # it, but an attribute credited to a subject must be written in that subject's own document.
        if not term_in_text(attribute.name, source_text) and not phrase_in_text(attribute.name, source_text):
            dropped.append(attribute.name)
            continue
        evidence = attribute.evidence
        if not phrase_in_text(evidence, source_text):
            evidence = snippet(attribute.name, source_text)
        if not any(same(attribute.name, existing.name) for existing in kept):
            kept.append(replace(attribute, evidence=evidence))

    for criterion in criteria or []:
        for term in [criterion.name, *criterion.related_terms]:
            if term_in_text(term, source_text) and not any(same(term, existing.name) for existing in kept):
                kept.append(Attribute(name=term, evidence=snippet(term, source_text), source="source_text"))
    return kept, dropped


def prematch(name: str, related_terms: list[str], attributes: list[Attribute]) -> list[str]:
    """Attributes whose name equals or contains the criterion or one of its related terms.

    Computed deterministically and shown to the model as a hint, and used as the fallback when the model
    cites nothing - so a plainly listed item is never silently lost.
    """
    terms = [normalize(term) for term in [name, *related_terms] if term.strip()]
    return [attribute.name for attribute in attributes
            if any(normalize(attribute.name) == term or term_in_text(term, attribute.name)
                   or term_in_text(attribute.name, term) for term in terms)]


def verify_grades(claims: list[GradeClaim], criteria: list[Criterion], attributes: list[Attribute],
                  policy: GroundingPolicy) -> tuple[list[GradeClaim], list[str]]:
    """One verified claim per criterion, in order. Returns (claims, corrections made).

    Two corrections, and they run in both directions on purpose:

    * a positive grade citing nothing real is demoted - otherwise the score rewards a sentence;
    * a criterion graded absent that the source does list becomes the "asserted" grade - otherwise the
      subject is punished for the model's oversight.

    Corrections are returned rather than logged away: they belong in the run's record, where a person
    reviewing the outcome can see what the deterministic layer had to fix.
    """
    by_index = {claim.criterion_index: claim for claim in claims}
    names = [attribute.name for attribute in attributes]
    verified: list[GradeClaim] = []
    corrections: list[str] = []
    for position, criterion in enumerate(criteria, start=1):
        claim = by_index.get(position) or GradeClaim(criterion_index=position, grade=policy.demoted_grade)
        cited = [name for name in names if any(same(name, claimed) for claimed in claim.cited)]
        fallback = prematch(criterion.name, criterion.related_terms, attributes)
        grade, evidence = claim.grade, claim.evidence
        if grade in policy.positive_grades and not cited:
            if fallback:
                cited = fallback
                evidence = evidence or f"listed: {', '.join(fallback)}"
            else:
                corrections.append(policy.demotion(criterion.name, grade))
                grade, evidence = policy.demoted_grade, ""
        elif grade == policy.demoted_grade and fallback:
            corrections.append(policy.assertion(criterion.name, fallback))
            cited, grade = fallback, policy.asserted_grade
            evidence = evidence or f"listed: {', '.join(fallback)}"
        verified.append(GradeClaim(criterion_index=position, grade=grade, cited=cited, evidence=evidence))
    return verified, corrections
