# agent service

AWS Strands screening agent (models through the LLM gateway, `ats_common.llm`, alias `ats-screening`) behind an MCP server (`/mcp`, streamable HTTP).

| MCP tool | Purpose |
|---|---|
| `screen_candidate(clientId, projectId, userId, requestId, chatId, jobDescription, resumeText, skillVersion?)` | Run the versioned screening skill; returns the camelCase ScreeningResult |
| `ask_agent(clientId, projectId, userId, requestId, question)` | Questions answered from the user's per-project screening memory |
| `cancel_request(requestId)` | Stop a running request (cancels the LLM call) |

* Zero trust: every call needs a service JWT (`aud=ats-agent`) from an allowed caller; tools are allow-listed per caller.
* Memory: Strands session repository on Postgres (`agent_*` tables) with a Redis write-through cache.
* Skills: `src/ats_agent/skills` (see its README). Screening logic: [docs/impl_screening.md](../../docs/impl_screening.md).

Local: `uv sync && PYTHONPATH=src uv run uvicorn ats_agent.server:app --port 8001` (env: `.env.example`).
