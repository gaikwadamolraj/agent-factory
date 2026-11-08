"""Recruitment's grounding: the ATS schemas mapped onto the shared `agent_common.match` checks.

The checks themselves - a criterion must be named by its source, an attribute must be written in the
subject's document, a positive grade must cite something real - are identical in every industry and live in
`agent_common.match`. This module is the translation: JD requirements in, criteria out; resume skills in,
attributes out; and the recruitment grade vocabulary supplied as policy.

Keeping the translation here is the point of the split. A grounding bug is fixed once, for recruitment,
insurance and banking at the same time, and recruitment's words never leak into the shared code.
"""
from agent_common.match import (Attribute, Criterion, GradeClaim, GroundingPolicy, normalize, phrase_in_text,
                                same, snippet, term_in_text)
from agent_common.match import prematch as _prematch
from agent_common.match import verify_attributes, verify_criteria, verify_grades

from agent_skill.schemas import CandidateProfile, JDAnalysis, MatchResult, ProfileSkill, RequirementMatch

# The ATS grade vocabulary, as the shared checks need it: which grades claim something positive, where an
# unevidenced claim lands, and what a merely-listed skill is worth. These names match the pack's rubric.
POSITIVE_GRADES = {"strong", "moderate", "claimed", "adjacent"}
ATS_POLICY = GroundingPolicy(positive_grades=frozenset(POSITIVE_GRADES), demoted_grade="absent",
                             asserted_grade="claimed",
                             demotion_note="{criterion}: {from} -> {to} (no real candidate skill cited)",
                             assertion_note="{criterion}: {from} -> {to} (resume names {names})")

# Re-exported so the steps and their tests keep one import for text checks.
__all__ = ["POSITIVE_GRADES", "normalize", "phrase_in_text", "prematch", "same", "snippet", "term_in_text",
           "verify_jd", "verify_matches", "verify_profile"]


def _criteria_of(analysis: JDAnalysis) -> list[Criterion]:
    return [Criterion(name=requirement.name, quote=requirement.jd_quote,
                      related_terms=list(requirement.related_terms))
            for requirement in analysis.requirements]


def _attributes_of(profile: CandidateProfile) -> list[Attribute]:
    return [Attribute(name=skill.name, evidence=skill.evidence, source=skill.source)
            for skill in profile.skills]


def verify_jd(analysis: JDAnalysis, jd_text: str) -> tuple[JDAnalysis, list[str]]:
    """Keep only requirements the JD actually names; de-duplicate. Returns (analysis, dropped names)."""
    kept, dropped = verify_criteria(_criteria_of(analysis), jd_text, ATS_POLICY)
    by_name = {normalize(criterion.name): criterion for criterion in kept}
    requirements = []
    for requirement in analysis.requirements:
        criterion = by_name.pop(normalize(requirement.name), None)
        if criterion is None:
            continue
        requirement.jd_quote = criterion.quote
        requirement.related_terms = criterion.related_terms
        requirements.append(requirement)
    return analysis.model_copy(update={"requirements": requirements}), dropped


def verify_profile(profile: CandidateProfile, resume_text: str,
                   analysis: JDAnalysis | None) -> tuple[CandidateProfile, list[str]]:
    """Drop skills not written in the resume; add JD terms that ARE written in it but were missed."""
    kept, dropped = verify_attributes(_attributes_of(profile), resume_text, ATS_POLICY,
                                      _criteria_of(analysis) if analysis else None)
    skills = [ProfileSkill(name=attribute.name, evidence=attribute.evidence,
                           source=attribute.source if attribute.source in _SOURCES else "resume_text")
              for attribute in kept]
    names = [skill.name for skill in skills]
    for role in profile.roles:
        role.skills_used = [used for used in role.skills_used if any(same(used, name) for name in names)]
    return profile.model_copy(update={"skills": skills}), dropped


_SOURCES = {"role", "project", "skills_list", "summary", "education", "certification", "resume_text"}


def prematch(req_name: str, related: list[str], skills: list[ProfileSkill]) -> list[str]:
    """Candidate skills whose name equals or contains the requirement or one of its related terms."""
    return _prematch(req_name, related,
                     [Attribute(name=skill.name, evidence=skill.evidence, source=skill.source)
                      for skill in skills])


def verify_matches(result: MatchResult, analysis: JDAnalysis,
                   profile: CandidateProfile) -> tuple[list[RequirementMatch], list[str]]:
    """One verified match per requirement. A positive grade must cite at least one real candidate skill."""
    claims = [GradeClaim(criterion_index=match.requirement_id, grade=match.grade,
                         cited=list(match.matched_skills), evidence=match.evidence)
              for match in result.matches]
    verified, corrections = verify_grades(claims, _criteria_of(analysis), _attributes_of(profile), ATS_POLICY)
    return [RequirementMatch(requirement_id=claim.criterion_index, grade=claim.grade,
                             matched_skills=claim.cited, evidence=claim.evidence)
            for claim in verified], corrections
