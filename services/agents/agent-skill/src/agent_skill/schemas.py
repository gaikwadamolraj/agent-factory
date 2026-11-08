"""Structured data of the screening skill: LLM step outputs and the final result returned over MCP."""
from typing import Literal, Optional

from pydantic import BaseModel, Field

from agent_common.models import ApiResponse

Grade = Literal["strong", "moderate", "adjacent", "claimed", "invalidated", "absent"]


# ---- Step 1: JD analysis
class JDRequirement(BaseModel):
    name: str = Field(description="One atomic skill/tool/credential/capability, worded as in the JD.")
    importance: Literal["must", "nice"] = "must"
    kind: Literal["skill", "tool", "credential", "experience", "education", "responsibility"] = "skill"
    jd_quote: str = Field(default="", description="Word-for-word phrase from the JD containing this item.")
    min_years: Optional[float] = None
    core: bool = Field(default=False, description="Central to the job title.")
    related_terms: list[str] = Field(default_factory=list,
                                     description="Synonyms, abbreviations, specific instances, same-ecosystem items.")


class JDAnalysis(BaseModel):
    role_title: str = ""
    sector: str = ""
    seniority: str = ""
    min_total_years: Optional[float] = None
    requirements: list[JDRequirement] = Field(default_factory=list)


# ---- Step 2: profile analysis
class ProfileSkill(BaseModel):
    name: str = Field(description="Skill/tool/method/credential exactly as spelled in the resume.")
    evidence: str = Field(default="", description="Verbatim resume snippet (max ~20 words) showing it.")
    source: Literal["role", "project", "skills_list", "summary", "education", "certification", "resume_text"] = "role"


class ProfileRole(BaseModel):
    title: str = ""
    organization: str = ""
    start: Optional[str] = Field(default=None, description="YYYY-MM or YYYY")
    end: Optional[str] = Field(default=None, description="YYYY-MM, YYYY, or 'present'")
    skills_used: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    roles: list[ProfileRole] = Field(default_factory=list)
    skills: list[ProfileSkill] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    manipulation_detected: bool = False
    manipulation_note: str = ""


# ---- Step 3: matching
class RequirementMatch(BaseModel):
    requirement_id: int = Field(description="The R-number of the requirement.")
    grade: Grade = "absent"
    matched_skills: list[str] = Field(default_factory=list, description="Names copied from the candidate skill list.")
    evidence: str = ""


class MatchResult(BaseModel):
    matches: list[RequirementMatch] = Field(default_factory=list)
    seniority_fit: int = Field(default=0, description="0, 5, 10 or 15")
    responsibility_fit: int = Field(default=0, description="0-15")
    education_fit: int = Field(default=10, description="0, 5 or 10")


# ---- Step 4: verdict
class ScreeningQuestion(BaseModel):
    skill: str
    question: str
    expected_answer: str
    red_flag: str = ""


class VerdictDraft(BaseModel):
    summary: str = ""
    recommendation_reason: str = ""
    questions: list[ScreeningQuestion] = Field(default_factory=list)


# ---- Final result (MCP response, camelCase on the wire)
class QuestionResult(ApiResponse):
    skill: str
    question: str
    expected_answer: str
    red_flag: str = ""


class RequirementResult(ApiResponse):
    name: str
    importance: Literal["must", "nice"]
    kind: str
    jd_quote: str
    grade: Grade
    matched_skills: list[str] = Field(default_factory=list)
    evidence: str = ""
    years: Optional[float] = None


class ScoreBreakdown(ApiResponse):
    must_have: float
    nice_to_have: float
    experience: float
    responsibilities: float
    education: float
    deductions: float
    cap: Optional[float] = None
    cap_reason: str = ""


class RoleResult(ApiResponse):
    """One role from the resume, kept with the result so date questions can be computed exactly later."""
    title: str = ""
    organization: str = ""
    start: Optional[str] = None
    end: Optional[str] = None


class ScreeningResult(ApiResponse):
    skill: str
    model: str
    role_title: str = ""
    sector: str = ""
    score: float
    band: str
    recommendation: str
    breakdown: ScoreBreakdown
    total_years: Optional[float] = None
    required_years: Optional[float] = None
    requirements: list[RequirementResult] = Field(default_factory=list)
    summary: str = ""
    recommendation_reason: str = ""
    questions: list[QuestionResult] = Field(default_factory=list)
    integrity_note: str = ""
    roles: list[RoleResult] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    agent_trace: dict = Field(default_factory=dict)
