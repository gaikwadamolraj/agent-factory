"""Typed client for the knowledge service. Any service can use it (install `agent-common[mcp]`).

    knowledge = KnowledgeClient(KnowledgeClientConfig(url=...), build_service_token_provider(settings))
    receipt = await knowledge.ingest(IngestRequest(clientId=1, projectId=2, namespace="talent",
                                                   sourceType="resume", sourceRef="document:42", text=resume,
                                                   guidance=PROFILE_GUIDANCE))
    result = await knowledge.search(SearchRequest(clientId=1, projectId=2, namespace="talent",
                                                  question="Who has led cloud migrations?"))

The caller must be on the knowledge service's ALLOWED_CALLERS and TOOL_POLICY lists.
"""
from dataclasses import dataclass

from agent_common.constants import JWT_AUDIENCE_KNOWLEDGE_GRAPH
from agent_common.knowledge.contracts import (
    EntityHit,
    EpisodeInfo,
    EpisodeList,
    ForgetRequest,
    ForgetResult,
    IngestReceipt,
    IngestRequest,
    ListEpisodesRequest,
    SearchRequest,
    SearchResult,
    UpdateEntityRequest,
)
from agent_common.knowledge.errors import KnowledgeErrors
from agent_common.mcp_client import McpTarget, McpToolClient
from agent_common.resilience import BreakerConfig
from agent_common.security import ServiceTokenProvider


@dataclass(frozen=True)
class KnowledgeClientConfig:
    url: str                        # e.g. http://agent-knowledge-graph/mcp
    timeout_s: float = 60.0         # search may make one LLM call; ingest only queues
    breaker_failures: int = 3
    breaker_reset_s: float = 30.0


class KnowledgeClient:
    """Knowledge-graph tools over MCP; `tokens` provides the calling service's tokens (aud=agent-knowledge-graph)."""

    def __init__(self, config: KnowledgeClientConfig, tokens: ServiceTokenProvider):
        self._tools = McpToolClient(McpTarget(
            name="knowledge", url=config.url, audience=JWT_AUDIENCE_KNOWLEDGE_GRAPH,
            timeout_s=config.timeout_s, unavailable=KnowledgeErrors.SERVICE_UNAVAILABLE,
            timeout=KnowledgeErrors.GRAPH_TIMEOUT,
            breaker=BreakerConfig(config.breaker_failures, config.breaker_reset_s)), tokens)

    async def ingest(self, request: IngestRequest) -> IngestReceipt:
        """Queue raw text for extraction into the tenant graph. Returns at once; extraction runs in the worker."""
        text = await self._tools.call("ingest_episode", {"request": request.model_dump(mode="json", by_alias=True)})
        return IngestReceipt.model_validate_json(text)

    async def status(self, client_id: int, project_id: int, namespace: str, episode_id: str) -> EpisodeInfo:
        text = await self._tools.call("get_episode", {"clientId": client_id, "projectId": project_id,
                                                      "namespace": namespace, "episodeId": episode_id})
        return EpisodeInfo.model_validate_json(text)

    async def search(self, request: SearchRequest) -> SearchResult:
        """Facts (and, unless answer=False, a grounded answer) from the tenant graph; semantic-cached."""
        text = await self._tools.call("search_knowledge", {"request": request.model_dump(mode="json", by_alias=True)})
        return SearchResult.model_validate_json(text)

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        """Remove everything learned from one source (e.g. a deleted resume) - right to erasure."""
        text = await self._tools.call("forget_source", {"request": request.model_dump(mode="json", by_alias=True)})
        return ForgetResult.model_validate_json(text)

    async def update_entity(self, request: UpdateEntityRequest) -> EntityHit:
        """Rename one entity and/or set its attributes (deterministic, no LLM). Raises KNW-NFD-0002 if unknown."""
        text = await self._tools.call("update_entity", {"request": request.model_dump(mode="json", by_alias=True)})
        return EntityHit.model_validate_json(text)

    async def list_episodes(self, request: ListEpisodesRequest) -> EpisodeList:
        """Episodes of a tenant graph (optionally one subject or source), newest first."""
        text = await self._tools.call("list_episodes", {"request": request.model_dump(mode="json", by_alias=True)})
        return EpisodeList.model_validate_json(text)

    async def cancel(self, request_id: str) -> bool:
        """Stop a running search (and its LLM call). Never raises."""
        try:
            return '"cancelled": true' in await self._tools.call("cancel_request", {"requestId": request_id})
        except Exception:
            return False
