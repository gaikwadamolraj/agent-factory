"""Process-wide runtime objects, created once at startup (dependency container)."""
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis

from agent_common.authz import AuthzService, database_snapshot_loader
from agent_common.db import Database, init_database
from agent_common.guardrails.client import GuardrailsClient, GuardrailsClientConfig, LocalGuardrails
from agent_common.knowledge.client import KnowledgeClient, KnowledgeClientConfig
from agent_common.security import ServiceTokenProvider, build_service_token_provider
from agent_common.workflow import JourneyLauncher, WorkflowClient
from ats_orchestrator.config import OrchestratorSettings
from ats_orchestrator.knowledge import TalentKnowledge
from ats_orchestrator.knowledge.pack_guidance import build_resolver
from ats_orchestrator.metrics import ActiveUsers, register_gauges
from ats_orchestrator.services.agent_client import AgentClient, ComputeClient
from ats_orchestrator.services.llm_queue import LLMRequestQueue, get_llm_queue
from ats_orchestrator.services.turn_registry import TurnRegistry


@dataclass
class Container:
    settings: OrchestratorSettings
    db: Database
    redis: aioredis.Redis
    agent: AgentClient
    compute: ComputeClient
    turns: TurnRegistry
    queue: LLMRequestQueue
    active_users: ActiveUsers
    knowledge: TalentKnowledge
    guardrails: GuardrailsClient | LocalGuardrails
    authz: AuthzService
    # Durable journeys. `launcher.enabled` is False when no engine is configured, and callers fall back to
    # their in-process path - the orchestrator must keep working without Temporal (docs/impl_temporal.md).
    workflows: WorkflowClient
    launcher: JourneyLauncher


def _guardrails(settings: OrchestratorSettings, tokens: ServiceTokenProvider) -> GuardrailsClient | LocalGuardrails:
    """agent-guardrail client, or local pattern checks when no service address is configured."""
    if not settings.guardrails_mcp_url:
        return LocalGuardrails()
    return GuardrailsClient(GuardrailsClientConfig(url=settings.guardrails_mcp_url,
                                                   timeout_s=settings.guardrails_timeout_s), tokens)


def _knowledge(settings: OrchestratorSettings, tokens: ServiceTokenProvider, redis) -> TalentKnowledge:
    """Talent knowledge on the knowledge-graph service (no client = disabled when no service address is set).

    The guidance resolver supplies the client's activated domain pack when it has a complete one; without a
    registry address, a token provider or Redis it always yields the packaged guidance and ontology
    (knowledge/pack_guidance.py).
    """
    client = KnowledgeClient(KnowledgeClientConfig(url=settings.knowledge_mcp_url,
                                                   timeout_s=settings.knowledge_timeout_s), tokens) \
        if settings.knowledge_mcp_url else None
    return TalentKnowledge(settings, client, build_resolver(settings, tokens, redis))


_container: Optional[Container] = None


async def init_container(settings: OrchestratorSettings) -> Container:
    global _container
    redis_client = aioredis.from_url(settings.redis_connection_url, decode_responses=True)
    await redis_client.ping()
    queue = get_llm_queue(settings.llm_max_concurrency)
    active_users = ActiveUsers(settings.active_user_window_s)
    register_gauges(active_users, lambda: queue.waiting)
    tokens = build_service_token_provider(settings)    # service tokens of this service for every MCP target
    db = init_database(settings)
    authz = AuthzService(redis_client, settings.app_env, database_snapshot_loader(db),
                         settings.authz_version_cache_ttl_s)
    # Constructing the client does not connect: a Temporal outage must not stop this service starting.
    workflows = WorkflowClient(settings.temporal_address, settings.temporal_namespace,
                               identity=settings.service_name,
                               connect_timeout_s=settings.temporal_connect_timeout_s,
                               call_timeout_s=settings.temporal_call_timeout_s)
    _container = Container(settings=settings, db=db, redis=redis_client,
                           agent=AgentClient(settings, tokens), compute=ComputeClient(settings, tokens),
                           turns=TurnRegistry(redis_client, settings.app_env),
                           queue=queue, active_users=active_users, knowledge=_knowledge(settings, tokens, redis_client),
                           guardrails=_guardrails(settings, tokens), authz=authz,
                           workflows=workflows, launcher=JourneyLauncher(db, workflows))
    return _container


async def close_container() -> None:
    global _container
    if _container is not None:
        await _container.workflows.close()
        await _container.redis.aclose()
        await _container.db.close()
    _container = None


def get_container() -> Container:
    if _container is None:
        raise RuntimeError("Container is not initialised.")
    return _container
