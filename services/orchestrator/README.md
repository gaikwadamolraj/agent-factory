# orchestrator service

FastAPI `/api/v1` + LangGraph conversational assistant.

| Endpoint | Notes |
|---|---|
| `POST /api/v1/auth/login` | public, throttled; returns JWT + user + client + projects |
| `GET /api/v1/auth/me` | session info |
| `POST/GET /api/v1/chats` | tenant headers `X-Client-Id`, `X-Project-Id` required |
| `GET/PATCH/DELETE /api/v1/chats/{chatId}` | detail (+ `runningTurnId`), rename / set `jobDescription`, delete |
| `POST /api/v1/chats/{chatId}/messages` | multipart `content`, `action` (chat/screen), `jobDescription?`, `file?` (PDF) -> SSE |
| `POST /api/v1/chats/{chatId}/turns/{turnId}/abort` | stop a running turn down to the LLM |

SSE events: `turn_started`, `user_message`, `progress`, `heartbeat`, then one of `message` / `aborted` / `error`.
Middleware order and tenancy: [docs/impl_tenancy_auth.md](../../docs/impl_tenancy_auth.md). Abort: [docs/impl_abort.md](../../docs/impl_abort.md).

Candidate knowledge graph (`src/orchestrator/knowledge/`, on when `KNOWLEDGE_MCP_URL` is set): a resume upload is
queued for the shared knowledge service in the background, screening outcomes follow, and the assistant gets the
`search_candidate_knowledge` tool. Guidance is versioned in `knowledge/guidance.py`. Details:
[docs/impl_knowledge_graph.md](../../docs/impl_knowledge_graph.md).

Local: `uv sync && PYTHONPATH=src uv run uvicorn orchestrator.main:app --port 5000` (env: `.env.example`).
