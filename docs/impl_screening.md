# Screening: autonomous agent + deterministic grounding

The Strands agent activates the versioned skill (`agent_skill/skills/ats_screening/vN.py`) and calls four tools.
Each tool = one schema-constrained LLM call followed by code checks, so the LLM can never invent facts that reach
the result.

| Step | LLM produces | Code checks (screening/grounding.py, scoring.py) |
|---|---|---|
| analyze_job_description | atomic requirements, related terms derived from the JD's sector | drop requirements the JD does not name (quote or key words must appear); de-duplicate |
| analyze_candidate_profile | skills with verbatim evidence, roles with dates | drop skills not written in the resume; add JD terms written verbatim in the resume (recall) |
| match_requirements | grade per requirement + cited candidate skills | a positive grade must cite a real candidate skill, else `absent`; years from role dates; score computed by a fixed rubric with caps |
| write_verdict | summary, recommendation reason, one question per must-have | facts block is computed by code; the model only writes prose |

If the agent skips a step, the service runs it (`stepsFallback` in the result, metric `ran_by=fallback`).
Results and the per-step caches are keyed by skill version + model + content hash.
