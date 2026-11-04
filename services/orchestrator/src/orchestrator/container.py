"""Process-wide runtime objects, created once at startup (dependency container)."""
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis

from ats_common.db import Database, init_database
from ats_common.knowledge.client import KnowledgeClient, KnowledgeClientConfig
from orchestrator.config import OrchestratorSettings
from orchestrator.constants import SERVICE_NAME_FOR_AGENT
from orchestrator.knowledge import TalentKnowledge
from orchestrator.metrics import ActiveUsers, register_gauges
from orchestrator.services.agent_client import AgentClient, ComputeClient
from orchestrator.services.llm_queue import LLMRequestQueue, get_llm_queue
from orchestrator.services.turn_registry import TurnRegistry


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


def _knowledge(settings: OrchestratorSettings) -> TalentKnowledge:
    client = KnowledgeClient(KnowledgeClientConfig(url=settings.knowledge_mcp_url, caller=SERVICE_NAME_FOR_AGENT,
                                                   timeout_s=settings.knowledge_timeout_s),
                             settings.require_jwt_secret(), settings.jwt_algorithm) \
        if settings.knowledge_mcp_url else None
    return TalentKnowledge(settings, client)


_container: Optional[Container] = None


async def init_container(settings: OrchestratorSettings) -> Container:
    global _container
    redis_client = aioredis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    await redis_client.ping()
    queue = get_llm_queue(settings.llm_max_concurrency)
    active_users = ActiveUsers(settings.active_user_window_s)
    register_gauges(active_users, lambda: queue.waiting)
    _container = Container(settings=settings, db=init_database(settings), redis=redis_client,
                           agent=AgentClient(settings), compute=ComputeClient(settings),
                           turns=TurnRegistry(redis_client, settings.app_env),
                           queue=queue, active_users=active_users, knowledge=_knowledge(settings))
    return _container


async def close_container() -> None:
    global _container
    if _container is not None:
        await _container.redis.aclose()
        await _container.db.close()
    _container = None


def get_container() -> Container:
    if _container is None:
        raise RuntimeError("Container is not initialised.")
    return _container
