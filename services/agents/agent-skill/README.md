# agent-skill

AWS Strands screening agent (models through the LLM gateway, `agent_common.llm`, alias `ats-screening`) behind an MCP server (`/mcp`, streamable HTTP).

| MCP tool | Purpose |
|---|---|
| `screen_candidate(clientId, projectId, userId, requestId, chatId, jobDescription, resumeText, skillVersion?)` | Run the versioned screening skill; returns the camelCase ScreeningResult |
| `ask_agent(clientId, projectId, userId, requestId, question)` | Questions answered from the user's per-project screening memory |
| `cancel_request(requestId)` | Stop a running request (cancels the LLM call) |

* Zero trust: every call needs a service JWT (`aud=agent-skill`) from an allowed caller; tools are allow-listed per caller.
* Memory: Strands session repository on Postgres (`agent_*` tables) with a Redis write-through cache.
* Skills: `src/agent_skill/skills` (see its README). Screening logic: [docs/impl_screening.md](../../docs/impl_screening.md).
* Matching and scoring are split: the industry-neutral half is `agent_common.match` (grounding) and
  `agent_common.rules` (deterministic scoring from a rubric); recruitment's half stays here in
  `screening/` — the date arithmetic, the JD/resume vocabulary, the words a recruiter reads, and the
  packaged rubric in `screening/rubric.py`. See [docs/impl_match_rules.md](../../../docs/impl_match_rules.md).
  A client whose activated version ships `rubrics/fit.rubric.json` is scored by that file instead.

Local: `uv sync && PYTHONPATH=src uv run uvicorn agent_skill.server:app --port 8001` (env: `.env.example`).
