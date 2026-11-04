"""The four screening steps: schema-constrained LLM call -> deterministic verification -> workspace.

The workspace is a plain JSON dict kept in the agent's state (persisted by the session manager), so a step can be
called by the agent through its tool, or directly by the service as a fallback when the agent skips it.
"""
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

import redis

from ats_agent import metrics
from ats_agent.config import AgentSettings
from ats_agent.llm import structured_call
from ats_agent.schemas import (
    CandidateProfile, JDAnalysis, MatchResult, RequirementResult, ScreeningResult, VerdictDraft,
)
from ats_agent.screening import grounding, scoring
from ats_agent.skills import SkillBundle
from ats_common.logging import get_logger, log_event
from ats_common.redis_keys import Keys, build_key, digest, ttl

log = get_logger(__name__)

ProgressFn = Callable[[str, str], Awaitable[None]]


class StepError(RuntimeError):
    """Recoverable problem (e.g. a step called out of order). The agent sees the message and can correct itself."""


@dataclass
class StepRuntime:
    """Per-request objects handed to tools through Strands' invocation_state (never persisted)."""
    settings: AgentSettings
    model: Any
    model_name: str
    skill: SkillBundle
    cache: Optional[redis.Redis] = None
    progress: Optional[ProgressFn] = None


@dataclass
class StepContext:
    workspace: dict[str, Any]
    runtime: StepRuntime

    async def report(self, step: str, message: str) -> None:
        if self.runtime.progress:
            await self.runtime.progress(step, message)

    def _key(self, kind: str, text: str) -> str:
        return build_key(Keys.AGENT_STEP_CACHE, self.runtime.settings.app_env, kind=kind,
                         digest=digest(self.runtime.skill.cache_id, self.runtime.model_name, text))

    def cache_get(self, kind: str, text: str) -> Optional[dict]:
        """Cached step output for identical input (skill version + model are part of the key)."""
        if not self.runtime.cache:
            return None
        try:
            raw = self.runtime.cache.get(self._key(kind, text))
            return json.loads(raw) if raw else None
        except Exception as e:  # the cache is only an optimisation
            log_event(log, "agent.cache_read_failed", error_type=type(e).__name__)
            return None

    def cache_set(self, kind: str, text: str, value: dict) -> None:
        if self.runtime.cache:
            try:
                self.runtime.cache.setex(self._key(kind, text), ttl(Keys.AGENT_STEP_CACHE), json.dumps(value))
            except Exception as e:
                log_event(log, "agent.cache_write_failed", error_type=type(e).__name__)

    def correct(self, notes: list[str]) -> None:
        self.workspace.setdefault("corrections", []).extend(notes)


def _require(ws: dict, *keys: str) -> None:
    missing = [k for k in keys if not ws.get(k)]
    if missing:
        hint = {"jd": "analyze_job_description", "profile": "analyze_candidate_profile", "match": "match_requirements",
                "jd_text": "a job description", "resume_text": "a resume"}
        raise StepError("Missing: " + ", ".join(hint.get(k, k) for k in missing) + ". Do that first.")


# ---------------------------------------------------------------- 1. JD
async def analyze_job_description(ctx: StepContext) -> str:
    ws = ctx.workspace
    _require(ws, "jd_text")
    await ctx.report("analyze_job_description", "Reading the job description and listing its requirements...")
    cached = ctx.cache_get("jd", ws["jd_text"])
    if cached:
        analysis = JDAnalysis.model_validate(cached)
    else:
        raw = await structured_call(ctx.runtime.settings, ctx.runtime.model,
                                    ctx.runtime.skill.render("JD_ANALYSIS", JD=ws["jd_text"]), JDAnalysis,
                                    "jd_analysis")
        analysis, dropped = grounding.verify_jd(raw, ws["jd_text"])
        ctx.correct([f"JD: dropped '{d}' (not named in the JD)" for d in dropped])
        ctx.cache_set("jd", ws["jd_text"], analysis.model_dump())
    ws["jd"] = analysis.model_dump()
    reqs = analysis.requirements
    musts = sum(r.importance == "must" for r in reqs)
    await ctx.report("analyze_job_description", f"Found {len(reqs)} requirements ({musts} must-have)")
    return (f"JD analysed: {analysis.role_title or 'role'} ({analysis.sector or 'sector not stated'}), "
            f"{len(reqs)} requirements ({musts} must-have): " + ", ".join(r.name for r in reqs[:15]))


# ---------------------------------------------------------------- 2. profile
async def analyze_candidate_profile(ctx: StepContext) -> str:
    ws = ctx.workspace
    _require(ws, "resume_text")
    await ctx.report("analyze_candidate_profile", "Extracting skills, roles and dates from the resume...")
    cached = ctx.cache_get("profile", ws["resume_text"])
    if cached:
        raw = CandidateProfile.model_validate(cached)
    else:
        raw = await structured_call(ctx.runtime.settings, ctx.runtime.model,
                                    ctx.runtime.skill.render("PROFILE_ANALYSIS", PROFILE=ws["resume_text"]),
                                    CandidateProfile, "profile_analysis")
        ctx.cache_set("profile", ws["resume_text"], raw.model_dump())
    analysis = JDAnalysis.model_validate(ws["jd"]) if ws.get("jd") else None
    profile, dropped = grounding.verify_profile(raw, ws["resume_text"], analysis)
    ctx.correct([f"Profile: dropped '{d}' (not written in the resume)" for d in dropped])
    ws["profile"] = profile.model_dump()
    years = scoring.total_years(profile)
    await ctx.report("analyze_candidate_profile", f"Verified {len(profile.skills)} skills across {len(profile.roles)} roles")
    return (f"Profile analysed: {len(profile.skills)} verified skills, {len(profile.roles)} roles, "
            f"{years if years is not None else 'unknown'} years. Skills: "
            + ", ".join(s.name for s in profile.skills[:20]))


# ---------------------------------------------------------------- 3. matching + score
def _requirement_lines(analysis: JDAnalysis, profile: CandidateProfile) -> str:
    lines = []
    for i, r in enumerate(analysis.requirements, start=1):
        pre = grounding.prematch(r.name, r.related_terms, profile.skills)
        lines.append(f"R{i} [{r.importance}] {r.name} (kind: {r.kind}"
                     + (f", min {r.min_years:g} yrs" if r.min_years else "")
                     + f") | related: {', '.join(r.related_terms) or '-'} | pre-matched: {', '.join(pre) or '-'}")
    return "\n".join(lines)


async def match_requirements(ctx: StepContext) -> str:
    ws = ctx.workspace
    _require(ws, "jd", "profile")
    analysis = JDAnalysis.model_validate(ws["jd"])
    profile = CandidateProfile.model_validate(ws["profile"])
    await ctx.report("match_requirements", f"Grading {len(analysis.requirements)} requirements against the profile...")

    prompt = ctx.runtime.skill.render(
        "MATCH",
        SECTOR=analysis.sector or "as implied by the requirements",
        SENIORITY=analysis.seniority or "not stated",
        REQUIREMENTS=_requirement_lines(analysis, profile) or "(none)",
        SKILLS="\n".join(f"- {s.name} | {s.source} | {s.evidence}" for s in profile.skills) or "(none)",
        ROLES="\n".join(f"- {r.title} @ {r.organization} ({r.start or '?'} - {r.end or '?'}): {', '.join(r.skills_used)}"
                        for r in profile.roles) or "(none)",
        EDUCATION="; ".join(profile.education + profile.certifications) or "(none)",
    )
    raw = await structured_call(ctx.runtime.settings, ctx.runtime.model, prompt, MatchResult, "matching")
    matches, corrections = grounding.verify_matches(raw, analysis, profile)
    ctx.correct(corrections)

    years_total = scoring.total_years(profile)
    results: list[RequirementResult] = []
    for req, m in zip(analysis.requirements, matches):
        yrs = scoring.skill_years(profile, m.matched_skills) if m.matched_skills else None
        if req.min_years and m.grade == "strong" and (yrs or 0) < req.min_years * 0.75:
            ctx.correct([f"{req.name}: strong -> moderate ({yrs or 0} of {req.min_years:g} required years)"])
            m.grade = "moderate"
        results.append(RequirementResult(
            name=req.name, importance=req.importance, kind=req.kind, jd_quote=req.jd_quote, grade=m.grade,
            matched_skills=m.matched_skills, evidence=m.evidence, years=yrs,
        ))

    score, breakdown = scoring.compute_score(
        analysis, matches, years_total, raw.seniority_fit, raw.responsibility_fit, raw.education_fit,
        profile.manipulation_detected,
    )
    ws["match"] = {"requirements": [r.model_dump() for r in results], "score": score,
                   "breakdown": breakdown.model_dump(), "total_years": years_total}
    gaps = [r.name for r in results if r.grade in ("absent", "invalidated", "adjacent")]
    await ctx.report("match_requirements", f"Score {score} ({scoring.band(score)}), {len(gaps)} gap(s)")
    return (f"Matched: score {score}/100 ({scoring.band(score)}). Gaps: {', '.join(gaps) or 'none'}. "
            f"Met: {', '.join(r.name for r in results if r.name not in gaps) or 'none'}.")


# ---------------------------------------------------------------- 4. verdict
def _question_skills(results: list[RequirementResult]) -> list[str]:
    musts = [r for r in results if r.importance == "must"] or results
    gaps_first = sorted(musts, key=lambda r: r.grade in ("strong", "moderate"))
    return [r.name for r in gaps_first[:10]]


async def write_verdict(ctx: StepContext) -> str:
    ws = ctx.workspace
    _require(ws, "jd", "profile", "match")
    analysis = JDAnalysis.model_validate(ws["jd"])
    profile = CandidateProfile.model_validate(ws["profile"])
    match = ws["match"]
    results = [RequirementResult.model_validate(r) for r in match["requirements"]]
    score = match["score"]
    rec = scoring.recommendation(score)
    await ctx.report("write_verdict", "Writing the recruiter summary and interview questions...")

    facts = [
        f"Role: {analysis.role_title} | Sector: {analysis.sector} | Seniority: {analysis.seniority}",
        f"Score: {score}/100 ({scoring.band(score)}) | Recommendation: {rec}",
        f"Experience: {match['total_years']} years (JD asks {analysis.min_total_years or 'n/a'})",
        f"Score cap: {match['breakdown'].get('cap_reason') or 'none'}",
        f"Integrity note: {profile.manipulation_note or 'none'}",
        "Requirements:",
        *[f"- {r.name} [{r.importance}] {scoring.GRADE_LABEL[r.grade]}" + (f": {r.evidence}" if r.evidence else "")
          for r in results],
        f"Education: {'; '.join(profile.education) or 'not stated'}",
        f"Certifications: {'; '.join(profile.certifications) or 'none'}",
    ]
    draft = await structured_call(ctx.runtime.settings, ctx.runtime.model, ctx.runtime.skill.render(
        "VERDICT", FACTS="\n".join(facts), RECOMMENDATION=rec,
        QUESTION_SKILLS="\n".join(f"   - {s}" for s in _question_skills(results))), VerdictDraft, "verdict")

    result = ScreeningResult(
        skill=ctx.runtime.skill.cache_id, model=ctx.runtime.model_name, role_title=analysis.role_title,
        sector=analysis.sector, score=score, band=scoring.band(score), recommendation=rec,
        breakdown=match["breakdown"], total_years=match["total_years"], required_years=analysis.min_total_years,
        requirements=results, summary=draft.summary, recommendation_reason=draft.recommendation_reason,
        questions=[q.model_dump() for q in draft.questions[:10]], integrity_note=profile.manipulation_note if profile.manipulation_detected else "",
        roles=[{"title": r.title, "organization": r.organization, "start": r.start, "end": r.end}
               for r in profile.roles],
        education=profile.education, certifications=profile.certifications,
    )
    ws["result"] = result.model_dump()
    await ctx.report("write_verdict", "Summary and questions ready")
    return f"Verdict written: {score}/100 ({result.band}), recommendation: {rec}, {len(result.questions)} questions."


STEPS: dict[str, Callable[[StepContext], Awaitable[str]]] = {
    "analyze_job_description": analyze_job_description,
    "analyze_candidate_profile": analyze_candidate_profile,
    "match_requirements": match_requirements,
    "write_verdict": write_verdict,
}
OUTPUT_KEY = {"analyze_job_description": "jd", "analyze_candidate_profile": "profile",
              "match_requirements": "match", "write_verdict": "result"}


async def run_step(step: str, ctx: StepContext, ran_by: str) -> str:
    """Run one step and record duration/outcome metrics (ran_by: 'agent' chose it, 'fallback' forced it)."""
    started, outcome = time.perf_counter(), "ok"
    try:
        return await STEPS[step](ctx)
    except StepError:
        outcome = "rejected"
        raise
    except BaseException as e:
        outcome = "cancelled" if type(e).__name__ == "CancelledError" else "error"
        raise
    finally:
        metrics.steps_total.add(1, {"step": step, "ran_by": ran_by, "outcome": outcome})
        metrics.step_duration.record(time.perf_counter() - started, {"step": step})
