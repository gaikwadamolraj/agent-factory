"""Knowledge service settings (environment only; secrets come from the k8s Secret)."""
from functools import lru_cache
from typing import Optional

from agent_common.config import McpServiceSettings
from pydantic import SecretStr


class KnowledgeSettings(McpServiceSettings):
    service_name: str = "agent-knowledge-graph"
    port: int = 8002

    # Graph store (FalkorDB). One graph per namespace + client + project.
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: Optional[str] = None
    falkordb_password: Optional[SecretStr] = None
    graph_cache_size: int = 64              # tenant graphs kept open per process (LRU)
    graph_timeout_s: float = 3600.0         # one episode extraction: 5-30 min for a resume on a local 14B model
    graph_breaker_failures: int = 5
    graph_breaker_reset_s: float = 60.0

    # LLM: gateway, model aliases (extraction / answer / embedding), timeouts -> agent_common.llm (ats-llm-config)
    llm_concurrency: int = 2                # parallel LLM calls in one extraction (Graphiti); keep below backend capacity
    graph_query_timeout_s: float = 60.0     # one graph search or delete
    episode_max_chars: int = 4_000          # one extraction per document section of at most this size
    subject_search_overfetch: int = 4       # subject-scoped search fetches limit x this, then keeps the subject's facts

    # Semantic cache (Redis 8 / Redis Stack with vector search, database 0)
    semantic_cache_enabled: bool = True
    semantic_cache_distance: float = 0.08   # cosine distance; lower = stricter match
    semantic_cache_ttl_s: int = 24 * 3600

    # Worker (ingestion queue = knowledge_episodes table, claimed with FOR UPDATE SKIP LOCKED)
    worker_concurrency: int = 1             # episodes in parallel per worker; each already uses llm_concurrency
    worker_poll_interval_s: float = 2.0
    worker_lease_s: int = 4200              # > graph_timeout_s; a crashed worker's episode is retried after this
    worker_max_attempts: int = 3
    worker_drain_s: float = 540.0           # on SIGTERM: time to finish running work; < terminationGracePeriodSeconds

    # Input limits
    max_text_chars: int = 100_000
    max_answer_facts: int = 25

    # Extraction's own output budget, separate from the platform's LLM_MAX_OUTPUT_TOKENS.
    #
    # They are different workloads. A chat reply is a few hundred tokens; extracting a graph from a resume is
    # a large nested JSON document - entities, relations and attributes - and 4096 tokens does not hold one.
    # When the budget runs out the model stops mid-structure and the JSON will not parse, which surfaces as
    # `KNW-INT-0001` after three identical attempts and an episode that never reaches the graph. Sharing one
    # number with chat means the smaller workload sets the ceiling for the larger one.
    #
    # Raising it is bounded by the *backend's* context window: ask for more than the model can produce and it
    # either refuses the request or truncates anyway. If the backend cannot go higher, reduce what is asked
    # for instead - a narrower ontology, or a smaller `max_text_chars` - rather than retrying into the same wall.
    extraction_max_output_tokens: int = 16_384

    # Zero trust (allowed_callers, tool_policy come from McpServiceSettings)
    allowed_callers: str = "ats-orchestrator"
    tool_policy: str = ("ats-orchestrator=ingest_episode|get_episode|search_knowledge|forget_source|"
                        "update_entity|list_episodes|cancel_request")


@lru_cache
def get_settings() -> KnowledgeSettings:
    return KnowledgeSettings()
