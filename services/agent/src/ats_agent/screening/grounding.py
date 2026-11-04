"""Deterministic checks that keep every LLM step tied to the real JD and resume text."""
import re

from ats_agent.schemas import CandidateProfile, JDAnalysis, MatchResult, ProfileSkill, RequirementMatch

POSITIVE_GRADES = {"strong", "moderate", "claimed", "adjacent"}
_STOPWORDS = {
    "a", "an", "and", "or", "of", "the", "to", "in", "on", "for", "with", "experience", "knowledge", "strong",
    "good", "proven", "ability", "skills", "skill", "years", "year", "must", "have", "nice", "plus", "required",
    "preferred", "understanding", "familiarity", "hands", "working", "mandatory", "compulsory", "compulsary",
    "important", "also",
}


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip()


def _key_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9.#+][a-z0-9+#./-]*", normalize(text))
    return {w.strip("./-") for w in words if w.strip("./-") not in _STOPWORDS and len(w.strip("./-")) > 1}


def term_in_text(term: str, text: str) -> bool:
    """Whole-term, case-insensitive match that is safe for names like '.NET', 'C++', 'C#', 'Node.js'."""
    t = normalize(term)
    if len(t) < 2:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9+#])", normalize(text)) is not None


def phrase_in_text(phrase: str, text: str) -> bool:
    return bool(phrase.strip()) and normalize(phrase) in normalize(text)


def snippet(term: str, text: str, width: int = 60) -> str:
    flat = normalize(text)
    match = re.search(re.escape(normalize(term)), flat)
    return flat[max(0, match.start() - width): match.end() + width].strip() if match else ""


def same(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


# ---------------------------------------------------------------- step 1: JD
def verify_jd(analysis: JDAnalysis, jd_text: str) -> tuple[JDAnalysis, list[str]]:
    """Keep only requirements the JD actually names; de-duplicate."""
    kept, dropped, seen = [], [], set()
    jd_words = _key_words(jd_text)
    for req in analysis.requirements:
        name_words = _key_words(req.name)
        grounded = phrase_in_text(req.jd_quote, jd_text) or term_in_text(req.name, jd_text) or bool(
            name_words and len(name_words & jd_words) / len(name_words) >= 0.6
        )
        key = normalize(req.name)
        if not grounded or not key or key in seen:
            dropped.append(req.name)
            continue
        if not phrase_in_text(req.jd_quote, jd_text):
            req.jd_quote = req.name
        req.related_terms = [t for t in dict.fromkeys(req.related_terms) if t.strip() and not same(t, req.name)][:8]
        seen.add(key)
        kept.append(req)
    return analysis.model_copy(update={"requirements": kept}), dropped


# ---------------------------------------------------------------- step 2: profile
def verify_profile(
    profile: CandidateProfile, resume_text: str, analysis: JDAnalysis | None
) -> tuple[CandidateProfile, list[str]]:
    """Drop skills not written in the resume; add JD terms that ARE written in it but were missed."""
    kept: list[ProfileSkill] = []
    dropped: list[str] = []
    for skill in profile.skills:
        if term_in_text(skill.name, resume_text) or phrase_in_text(skill.name, resume_text):
            if not phrase_in_text(skill.evidence, resume_text):
                skill.evidence = snippet(skill.name, resume_text)
            if not any(same(skill.name, k.name) for k in kept):
                kept.append(skill)
        else:
            dropped.append(skill.name)

    if analysis:  # recall: requirement names / related terms written verbatim in the resume
        for req in analysis.requirements:
            for term in [req.name, *req.related_terms]:
                if term_in_text(term, resume_text) and not any(same(term, k.name) for k in kept):
                    kept.append(ProfileSkill(name=term, evidence=snippet(term, resume_text), source="resume_text"))

    names = [k.name for k in kept]
    for role in profile.roles:
        role.skills_used = [s for s in role.skills_used if any(same(s, n) for n in names)]
    return profile.model_copy(update={"skills": kept}), dropped


def prematch(req_name: str, related: list[str], skills: list[ProfileSkill]) -> list[str]:
    """Candidate skills whose name equals or contains the requirement or one of its related terms."""
    terms = [normalize(t) for t in [req_name, *related] if t.strip()]
    return [s.name for s in skills
            if any(normalize(s.name) == t or term_in_text(t, s.name) or term_in_text(s.name, t) for t in terms)]


# ---------------------------------------------------------------- step 3: matching
def verify_matches(
    result: MatchResult, analysis: JDAnalysis, profile: CandidateProfile
) -> tuple[list[RequirementMatch], list[str]]:
    """One verified match per requirement. A positive grade must cite at least one real candidate skill."""
    by_id = {m.requirement_id: m for m in result.matches}
    skill_names = [s.name for s in profile.skills]
    verified, corrections = [], []
    for i, req in enumerate(analysis.requirements, start=1):
        match = by_id.get(i) or RequirementMatch(requirement_id=i, grade="absent")
        cited = [n for n in skill_names if any(same(n, c) for c in match.matched_skills)]
        fallback = prematch(req.name, req.related_terms, profile.skills)
        if match.grade in POSITIVE_GRADES and not cited:
            if fallback:
                cited = fallback
                match.evidence = match.evidence or f"listed: {', '.join(fallback)}"
            else:
                corrections.append(f"{req.name}: {match.grade} -> absent (no real candidate skill cited)")
                match.grade, match.evidence = "absent", ""
        elif match.grade == "absent" and fallback:
            corrections.append(f"{req.name}: absent -> claimed (resume names {', '.join(fallback)})")
            cited, match.grade = fallback, "claimed"
            match.evidence = match.evidence or f"listed: {', '.join(fallback)}"
        match.matched_skills = cited
        verified.append(match)
    return verified, corrections
