"""Grounding and scoring are deterministic: these are the guarantees the LLM cannot break."""
from ats_agent.schemas import (CandidateProfile, JDAnalysis, JDRequirement, MatchResult, ProfileRole, ProfileSkill,
                               RequirementMatch)
from ats_agent.screening import grounding, scoring

JD = ".net and angular - mandatory\nmainframe also important\nsalesforce, snowflake - mandatory\nhtml css - compulsary"
RESUME = "Senior AI Engineer (2019 - Present): LangGraph agents, Python, FastAPI. Skills: Python, LangGraph, HTML."


def _jd(*names: str) -> JDAnalysis:
    return JDAnalysis(requirements=[JDRequirement(name=n, jd_quote=n) for n in names])


def test_requirements_not_named_in_jd_are_dropped():
    analysis, dropped = grounding.verify_jd(_jd(".NET", "Kubernetes", "Salesforce"), JD)
    assert [r.name for r in analysis.requirements] == [".NET", "Salesforce"]
    assert dropped == ["Kubernetes"]


def test_skills_not_written_in_resume_are_dropped():
    profile = CandidateProfile(skills=[ProfileSkill(name=n) for n in [".NET", "Salesforce", "Python", "HTML"]])
    verified, dropped = grounding.verify_profile(profile, RESUME, None)
    assert [s.name for s in verified.skills] == ["Python", "HTML"]
    assert set(dropped) == {".NET", "Salesforce"}


def test_positive_grade_without_real_evidence_becomes_absent():
    analysis, _ = grounding.verify_jd(_jd(".NET", "HTML"), JD)
    profile, _ = grounding.verify_profile(CandidateProfile(skills=[ProfileSkill(name="HTML")]), RESUME, analysis)
    llm = MatchResult(matches=[RequirementMatch(requirement_id=1, grade="strong", matched_skills=[".NET"]),
                               RequirementMatch(requirement_id=2, grade="strong", matched_skills=["HTML"])])
    matches, corrections = grounding.verify_matches(llm, analysis, profile)
    assert [m.grade for m in matches] == ["absent", "strong"]
    assert corrections and ".NET" in corrections[0]


def test_term_matching_respects_boundaries():
    assert grounding.term_in_text(".NET", "ASP.NET Core and .NET 8")
    assert grounding.term_in_text("C++", "Modern C++ developer")
    assert not grounding.term_in_text("Java", "JavaScript only")


def test_years_merge_overlapping_roles():
    profile = CandidateProfile(roles=[ProfileRole(start="2015-01", end="2019-12"), ProfileRole(start="2018-01", end="2020-12")])
    assert scoring.total_years(profile) == 6.0


def test_cap_when_half_of_must_haves_absent():
    analysis = _jd("A", "B")
    matches = [RequirementMatch(requirement_id=1, grade="strong"), RequirementMatch(requirement_id=2, grade="absent")]
    score, breakdown = scoring.compute_score(analysis, matches, None, 15, 15, 10, False)
    assert score == 40.0 and breakdown.cap == 40.0
