# User journeys: what runs, where data is stored, what the LLM sees

One page per journey, as the code works **today** (prompt `ASSISTANT_V6`). Each journey lists the steps in order,
the store each step reads or writes, every LLM call, and the exact prompt layout. Deeper details:
[impl_screening.md](impl_screening.md), [impl_documents_search.md](impl_documents_search.md),
[impl_knowledge_graph.md](impl_knowledge_graph.md), [impl_abort.md](impl_abort.md), [llm_gateway.md](llm_gateway.md).

```
browser ─► ats-orchestrator (FastAPI, SSE) ──MCP──► agent-skill      (screening, Strands agent, 4 steps)
                │                          ──MCP──► agent-compute ──► agent-sandbox   (calculate tool only)
                │                          ──MCP──► agent-knowledge-graph (graph search) ◄─ queue ─ agent-knowledge-graph-worker
                ├─► Postgres  chats, chat_messages, documents, document_blobs, document_chunks (pgvector + tsv),
                │             screenings, knowledge_episodes, agent_sessions/agent_states/agent_messages
                ├─► Redis     screening-result cache, abort flags, membership cache
                └─► llm-gateway (LiteLLM) ─► qwen3-4b (fast, 8k) | qwen2.5-14b (65k) | nomic-embed
```

## Stores at a glance

| Store | Table / key | Written by | Read by |
|---|---|---|---|
| Original files | `documents` (text, hash, filename) + `document_blobs` (bytes) | upload | resume viewer, screening, calculate |
| Search index | `document_chunks`: 1,200-char chunks, 200 overlap, `embedding` (768-d nomic) + `tsv` | upload | `search_documents`, grounding |
| Chat | `chats` (resume/JD ids, `jd_text`, `last_score`), `chat_messages` (role, content, `data` JSON) | every turn | history for the next turn |
| Screening | `screenings.result` (full JSON: requirements, roles, questions, breakdown) | screening | `get_screening_details`, work history, calculate |
| Screening cache | Redis `SCREENING_RESULT` (tenant + skill + JD hash + resume hash + model identity) | screening | the same screening again |
| Knowledge graph | FalkorDB graph `talent__c{client}_p{project}`; queue `knowledge_episodes` | knowledge worker | `search_candidate_knowledge` |
| Agent memory | `agent_sessions/agent_states/agent_messages` (per user + project) | agent-skill | `ask_screening_agent` |
| Chat memory | `chat_memory` (summary + notes of the recruiter's messages, `last_message_id`) | after each ask turn (background) | the next ask turn |
| Turn records | `chat_turns` (one JSON record per turn: journey, skill, steps, tools, grounding, memory, security, model, outcome, duration) | end of every turn | audit, debugging, analytics |

---

Every turn is one **journey** (`assistant/journeys.py`): `screen` (button) or `ask` (chat question). A journey
names its skill and what it uses (chat model, grounding, memory); the turn record says which journey and skill ran.

## Journey 1: Upload a resume (and JD)

`POST /api/v1/chats/{id}/messages` (multipart) → `runner._store_inputs`

1. **Parse.** PDF → text (`services/pdf.py`, in a thread). A JD may also be pasted text or a TXT/MD file.
2. **Deduplicate.** SHA-256 of the text; the same text in the same tenant reuses the stored document (no re-index).
3. **Store.** The original file goes to `document_blobs`; text and metadata go to `documents`.
4. **Chunk and index.** `chunk_text` (1,200 chars, 200 overlap, cut at paragraph/line/space). Each chunk is embedded
   through the gateway (`search_document: ` prefix) and stored in `document_chunks` with its `tsv`. If embedding
   fails, chunks are stored without vectors and keyword search still works.
5. **Knowledge graph (background).** `TalentKnowledge.remember_profile` queues an episode (`knowledge_episodes`)
   with the career ontology (`ontologies/talent_v2.json`) and `subject_ref=document:N`. The worker splits the
   resume into sections (≤4,000 chars) and extracts each with Graphiti (typed Candidate, Role, Organization,
   Skill, Tool, Credential, ... nodes; several LLM calls per section; minutes on local models), recording each
   finished section so a retry resumes. The chat does **not** wait for this.
6. **Record the turn.** `chats.resume_document_id` / `jd_document_id` / `jd_text` are set; the user message is stored
   with `data.attachments`.

LLM calls in the request: embeddings only. Extraction runs later in the worker.

## Journey 2: Screen the candidate

Button "Screen candidate" (`action=screen`) → `perform_screening`; or the chat model calls `screen_candidate`.

1. Load `chats.jd_text` and the resume text. Missing → friendly `JD_MISSING` / `RESUME_MISSING`.
2. **Cache.** Redis key from JD hash + resume hash + skill version + model identity (`ats-screening@<config version>`).
   A hit returns the stored result immediately.
3. **agent-skill over MCP** (`screen`): a Strands agent runs the `ats_screening` skill, four steps in order. Each step
   is one schema-constrained LLM call (`ats-screening`, the 14B) followed by deterministic checks:
   `analyze_job_description` → `analyze_candidate_profile` → `match_requirements` (every quote must exist in
   the text, `screening/grounding.py`) → `write_verdict` (score computed in code, `screening/scoring.py`).
   If the agent skips a step, the service runs it directly (fallback), so a result always comes back.
4. **Store.** Row in `screenings` (full result JSON), `chats.last_score`, Redis cache.
5. **Knowledge graph (background).** `remember_screening` queues a short outcome episode (no JD text).
6. The reply is built in code (`_screening_reply`), not by the chat model.

Progress events stream to the browser (`agent:analyze_job_description`, ...). Abort cancels the MCP call, and the
agent closes its LLM stream.

## Journey 3: Ask a question in the chat

`action=chat` → `runner._answer` → LangGraph assistant (`assistant/graph.py`).

### 3.1 What happens, in order
1. Read the chat, its resume document, its latest screening, its **chat memory** and its **last 4 messages**
   (`history_messages`).
2. **Ground (every turn, no model decision).** `runner._ground`: hybrid search for the user's question over this
   chat's resume and JD, top 3 chunks (`grounding_chunks`), plus the **first chunk of each document always**
   (name, title, location, contact details), cut at 5,000 chars (`grounding_max_chars`).
   Hybrid = vector ranking + keyword ranking (any word of the question), fused with RRF.
   **In parallel**, the knowledge graph gives facts about **this chat's candidate only** (`candidate_facts`:
   subject `document:N`, typed attributes such as `Candidate.location_text`, current facts; 8 lines, 4 s timeout,
   skipped when the graph has nothing yet). They are appended as `[knowledge graph: this candidate]`.
3. Build the prompt (3.2) and call the chat model (`ats-chat`: the 4B, or the 14B when the prompt is longer than
   4,096 tokens; the `high-quality` client profile uses the 14B only).
4. The model either answers or calls tools; the loop is bounded (at most 3 tool results, a shared step budget,
   identical repeated tool calls are not run again). Then it must answer.
5. Store the assistant message with `data.toolsUsed`.

### 3.2 Prompt layout (exactly what the model receives)
```
system : ASSISTANT_V7                      tools, tool-choice rules, "answer from DOCUMENT EXCERPTS",
                                           "never take facts from a file name", scores only from stored screenings
system : CHAT CONTEXT                      JD (length + first 300 chars), resume uploaded yes/no,
                                           latest screening (score, band, recommendation, role),
                                           work history from the latest screening (title at org, start - end)
                                           [SECURITY NOTE when the message looks like an injection]
                                           CONVERSATION MEMORY: summary + notes of the recruiter's earlier messages
user   : <message>                         \
assistant: <answer>                         |  last 4 stored messages, as plain text
...                                         |  (tool calls/results of earlier turns are NOT kept)
user   : <current question>                /
         + "DOCUMENT EXCERPTS retrieved for this question" + <untrusted-documents-NONCE> [resume #0] ...
           [jd #0] ... [knowledge graph: this candidate] ... </untrusted-documents-NONCE>
```
The excerpts are added to the **prompt only**. The stored message stays as the user typed it.

### 3.3 Tools the model can call
| Tool | Does | Stores used |
|---|---|---|
| `search_documents(query, source)` | Same hybrid search as grounding, headers pinned, up to 8,000 chars | `document_chunks` |
| `get_screening_details(section)` | requirements / gaps / questions / summary / breakdown | `screenings.result` |
| `screen_candidate()` | Journey 2 | as Journey 2 |
| `calculate(question)` | An LLM writes Python, agent-compute runs it in agent-sandbox, retried with the error up to 3 times | `screenings.result.roles` + resume text |
| `search_candidate_knowledge(question)` | Graph facts across the project's candidates | FalkorDB via agent-knowledge-graph |
| `ask_screening_agent(question)` | The agent's memory of this user's earlier screenings | `agent_*` tables |

### 3.4 Chat history and memory
- **Memory** (`assistant/memory.py`): after each ask turn, in the background, the recruiter's messages newer than
  `last_message_id` are folded into a summary (≤80 words) + notes (focus, decisions, answer style) by one
  structured call of the chat model. Only the **recruiter's** messages go in, never the assistant's answers, and
  messages that look like injections are skipped. Old chats build their memory on their next turn.
- **Recent messages:** only the last 4 messages go in verbatim (`history_messages`); older turns live in the memory.
- Why: on 2026-11-04 a chat kept repeating its own earlier wrong answers ("Amol Fadde", "London not mentioned")
  from 20 verbatim messages. Candidate facts now come only from documents, screening and the graph on every turn.
- **After the reply:** the score guard checks the answer (docs/security_llm.md), then the turn record is written to
  `chat_turns`.

## Journey 4: Ask across candidates (knowledge graph)
(Facts about the chat's own candidate already arrive with every turn, see 3.1 step 2.)

1. The chat model calls `search_candidate_knowledge`. The orchestrator calls agent-knowledge-graph `search_knowledge` over MCP
   with tenant headers and a service JWT (`aud=agent-knowledge-graph`).
2. agent-knowledge-graph: semantic cache lookup (Redis 8 vectors, scoped by tenant + graph version + answer model) →
   Graphiti hybrid search on the tenant's graph → facts. The orchestrator asks for facts only (`answer=false`).
3. The facts go back to the chat model as the tool result. A graph that is not built yet returns "no facts yet,
   use search_documents".

## Journey 5: Abort
The browser posts `.../turns/{turnId}/abort` or disconnects → the turn task is cancelled → in-flight agent / knowledge
requests are cancelled over MCP (the agent closes its LLM stream) → "Stopped..." is stored → `aborted` event.
Another replica sees the abort through a Redis flag. Details: [impl_abort.md](impl_abort.md).

---

## Incident 2026-11-04: "what is the candidate name" → "Amol Fadde"

| Finding | Evidence | Fix |
|---|---|---|
| The 4B chat model never called a tool; it answered from the prompt alone | `toolsUsed: []`, about 1 s answers; replay: 3 of 3 runs had no tool call (the 14B did call `search_documents`) | Grounding before the model (3.1 step 2) |
| The file name was the only "resume fact" in the prompt (`Amol_FDE.pdf` → "Fadde") | `CHAT CONTEXT - Resume: Amol_FDE.pdf` | File name removed from the prompt |
| Similarity search did not return the header chunk for "name" / "location" | top hits were chunks 8, 3, 7 | First chunk of each document is always pinned |
| The keyword half of hybrid search never matched a question | `websearch_to_tsquery` ANDs every word: `'candid' & 'name'` → 0 rows | Any-word (OR) query, quoted lexemes |
| Excerpts in the system message lost to wrong answers in the history | A/B test on the same chat: the system-context placement failed, excerpts after the question passed | Excerpts attached to the current question (V6) |
| "Current employer" was not in any retrieved chunk | the answer said Cognizant | Work history from the screening in CHAT CONTEXT |

Verified after the fix (fresh chat, 4B, about 2 s each): name Amol Gaikwad, London UK, phone, current employer
Publicis Sapient, 14+ years, Kubernetes with quotes. Still open: the history problem (3.4).
