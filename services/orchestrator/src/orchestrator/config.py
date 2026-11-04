"""Orchestrator settings (environment only; secrets come from the k8s Secret at runtime)."""
from functools import lru_cache

from ats_common.config import CommonSettings


class OrchestratorSettings(CommonSettings):
    service_name: str = "ats-orchestrator"

    # Assistant turns running at once; others queue FIFO. Models, gateway and timeouts: ats_common.llm (ats-llm-config)
    llm_max_concurrency: int = 5

    # Screening agent (MCP)
    agent_mcp_url: str
    agent_timeout_s: float = 1800.0         # a full screening on an 8B model takes minutes
    agent_connect_timeout_s: float = 30.0
    agent_breaker_failures: int = 3
    agent_breaker_reset_s: float = 30.0

    # Compute agent (ats-compute): runs code written by the calculate tool in the sandbox
    compute_mcp_url: str = "http://ats-compute/mcp"
    compute_timeout_s: float = 60.0

    # Shared knowledge service (MCP). Empty URL = disabled; screening and chat work without it.
    knowledge_mcp_url: str = ""
    knowledge_namespace: str = "talent"     # this use case's graphs: talent__c{client}_p{project}
    knowledge_guidance_version: str = ""    # empty = latest released (orchestrator/knowledge/guidance.py)
    knowledge_ingest_screenings: bool = True   # also store screening outcomes (score, met, gaps) for matching
    knowledge_timeout_s: float = 120.0
    knowledge_search_facts: int = 10        # graph facts given to the assistant per question
    knowledge_grounding_facts: int = 8      # facts about this chat's candidate added to every chat turn
    knowledge_grounding_timeout_s: float = 4.0   # grounding skips the graph if it is slower (never delays a turn)
    knowledge_max_text_chars: int = 100_000

    # Loop limits for one user request, across all agents. A step is one assistant LLM call, one tool call,
    # one code-author call or one code run. When it is spent, tools stop and the assistant answers.
    max_steps_per_request: int = 10
    max_code_attempts: int = 3              # code-author tries (write, run, fix with the error, run again)
    max_calculation_resume_chars: int = 20_000   # resume text given to the code author
    skill_version: str = ""                 # empty = agent default

    # Auth (httpOnly cookies: short access token + rotating refresh token)
    access_token_minutes: int = 5            # short: the UI refreshes silently at 80% of it
    refresh_token_days: int = 1              # rotated on every use; a stolen one dies within a day
    cookie_secure: bool = True              # set false only for plain-http local dev
    cookie_samesite: str = "strict"
    login_max_failures: int = 5             # per username per 15 minutes

    # Documents: original files in a pluggable blob store (embeddings: ats_common.llm)
    blob_backend: str = "postgres"
    reject_injected_documents: bool = True  # refuse resumes/JDs whose text tries to instruct the AI (FILE-INV-0004)
    search_results: int = 5                 # ranked chunks per search (the document headers are added on top)
    search_max_chars: int = 8_000           # excerpt text one search_documents call returns to the assistant
    # Grounding: before the assistant runs, the chunks for the user's question (+ document headers) go into its
    # context, so answers about the candidate never depend on the model choosing to search. Sized for the small
    # chat backend (4k input tokens): 3 chunks + headers ~ 1.3k tokens.
    grounding_chunks: int = 3
    grounding_max_chars: int = 5_000

    # API
    cors_origins: str = ""                  # comma-separated; required in PROD
    max_pdf_mb: int = 10                    # per file (resume PDF / JD file)
    max_request_mb: int = 25                # whole request (two files + form fields)
    max_message_chars: int = 4_000
    max_jd_chars: int = 50_000
    max_title_chars: int = 200
    history_messages: int = 4               # recent messages sent verbatim; older ones live in the chat memory
    memory_enabled: bool = True             # running summary of the recruiter's messages (assistant/memory.py)
    memory_max_new_messages: int = 20       # recruiter messages folded into the memory in one update
    memory_max_tokens: int = 400            # output budget of one memory update
    heartbeat_seconds: float = 2.0
    active_user_window_s: int = 900         # "active users" = seen in the last 15 minutes

    @property
    def max_pdf_bytes(self) -> int:
        return self.max_pdf_mb * 1024 * 1024

    @property
    def max_request_bytes(self) -> int:
        return self.max_request_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return origins if (origins or self.is_prod) else ["*"]


@lru_cache
def get_settings() -> OrchestratorSettings:
    return OrchestratorSettings()
