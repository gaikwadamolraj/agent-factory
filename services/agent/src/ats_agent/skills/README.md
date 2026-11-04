# Skills

A skill version is a Python module `ats_agent/skills/<name>/<version>.py` with:

| Constant           | Used by | Purpose |
|--------------------|---------|---------|
| `SKILL_MD`         | Strands `AgentSkills` plugin | Frontmatter (`name`, `description`) + workflow the agent follows after activating the skill |
| `JD_ANALYSIS`      | tool `analyze_job_description` | JD -> atomic requirements (`{{JD}}`) |
| `PROFILE_ANALYSIS` | tool `analyze_candidate_profile` | Resume -> verified skills and roles (`{{PROFILE}}`) |
| `MATCH`            | tool `match_requirements` | Grades every requirement (`{{REQUIREMENTS}}`, `{{SKILLS}}`, ...) |
| `VERDICT`          | tool `write_verdict` | Summary + interview questions (`{{FACTS}}`, ...) |

## Rules

* **Never edit a released version.** Copy `v1.py` to `v2.py`, change it, then select it:
  `SKILL_VERSION=v2 ./deploy.sh orchestrator` (per request) or `DEFAULT_SKILL_VERSION` on the agent.
  An unknown version falls back to the default, so a typo never breaks screening.
* Keep prompts domain-agnostic: no technology or industry names. The model derives related skills, tools,
  standards and regulations from the JD's own sector.
* The skill's `cache_id` (name@version:content-hash) is part of every cache key, so changes never serve stale results.
* Prompts only produce data. The scoring rubric and the grounding checks are code (`screening/`), so they behave
  the same for every skill version.

## Changelog

| Version | Change |
|---------|--------|
| ats_screening v1 | Multi-step skill: JD analysis (atomic, quoted requirements), profile analysis (verbatim evidence), matching with sector-derived related terms, verdict with one question per must-have. |
