"""Orchestrator settings (environment only; secrets come from the k8s Secret at runtime)."""
from functools import lru_cache
from typing import Optional

from ats_common.config import CommonSettings


class OrchestratorSettings(CommonSettings):
    service_name: str = "ats-orchestrator"

    # Assistant LLM (LM Studio / OpenAI-compatible)
    llm_endpoint: str
    llm_api_key: str = "not-needed"
    model_name: str = "meta-llama-3.1-8b-instruct"
    llm_timeout_s: float = 300.0
    llm_max_concurrency: int = 5            # turns running at once (= LM Studio --parallel); others queue FIFO
    llm_breaker_failures: int = 5
    llm_breaker_reset_s: float = 30.0

    # Screening agent (MCP)
    agent_mcp_url: str
    agent_timeout_s: float = 1800.0         # a full screening on an 8B model takes minutes
    agent_connect_timeout_s: float = 30.0
    agent_breaker_failures: int = 3
    agent_breaker_reset_s: float = 30.0

    # Compute agent (ats-compute): runs code written by the calculate tool in the sandbox
    compute_mcp_url: str = "http://ats-compute/mcp"
    compute_timeout_s: float = 60.0

    # Loop limits for one user request, across all agents. A step is one assistant LLM call, one tool call,
    # one code-author call or one code run. When it is spent, tools stop and the assistant answers.
    max_steps_per_request: int = 10
    max_code_attempts: int = 3              # code-author tries (write, run, fix with the error, run again)
    max_calculation_resume_chars: int = 20_000   # resume text given to the code author
    skill_version: str = ""                 # empty = agent default

    # Auth (httpOnly cookies: short access token + rotating refresh token)
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cookie_secure: bool = True              # set false only for plain-http local dev
    cookie_samesite: str = "strict"
    login_max_failures: int = 5             # per username per 15 minutes

    # Documents: embeddings for hybrid search, original files in a pluggable blob store
    embedding_endpoint: Optional[str] = None
    embedding_model: str = ""
    blob_backend: str = "postgres"
    search_results: int = 5

    # API
    cors_origins: str = ""                  # comma-separated; required in PROD
    max_pdf_mb: int = 10                    # per file (resume PDF / JD file)
    max_request_mb: int = 25                # whole request (two files + form fields)
    max_message_chars: int = 4_000
    max_jd_chars: int = 50_000
    max_title_chars: int = 200
    history_messages: int = 20
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

    @property
    def embeddings_url(self) -> str:
        return self.embedding_endpoint or self.llm_endpoint


@lru_cache
def get_settings() -> OrchestratorSettings:
    return OrchestratorSettings()
