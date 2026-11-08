"""Agent use cases exposed over MCP: screen a candidate, answer questions from memory, cancel a running request.

Cancellation: every request registers its asyncio task under the caller's `request_id`. `cancel(request_id)`
cancels that task; the CancelledError propagates through the Strands loop and the LLM HTTP call (closing the
connection makes LM Studio stop generating), so an aborted turn stops spending tokens immediately.
"""
import asyncio
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import redis

from agent_skill import metrics
from agent_skill.agent_factory import build_agent, session_id_for
from agent_skill.config import AgentSettings
from agent_skill.errors import AgentErrors
from agent_skill.llm import build_model
from agent_skill.memory.repository import PostgresRedisSessionRepository
from agent_skill.schemas import ScreeningResult
from agent_skill.screening.steps import OUTPUT_KEY, STEPS, ProgressFn, StepContext, StepRuntime, run_step
from agent_skill.skills import load_skill
from agent_skill.skills.registry_source import RegistrySkillSource, build_skill_registry_target
from agent_skill.tools import HISTORY, WORKSPACE
from agent_common.context import bind
from agent_common.errors import AppError, Errors
from agent_common.llm import Purpose, model_identity
from agent_common.logging import get_logger, log_event
from agent_common.resilience import RetryPolicy, is_llm_format_error, retry
from agent_common.security import build_service_token_provider

log = get_logger(__name__)

MODEL_FORMAT_RETRY = RetryPolicy(attempts=2, base_delay_s=1.0, max_delay_s=4.0, retry_on=(),
                                 retry_if=is_llm_format_error)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_REGISTRY_VERSION = re.compile(r"^v?(\d{1,5})$")


def _version_number(version: str) -> Optional[int]:
    """"3" / "v3" -> 3 (a registry version number); anything else -> None (a packaged module name)."""
    match = _REGISTRY_VERSION.match(version or "")
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class Tenant:
    client_id: int
    project_id: int
    user_id: int

    @property
    def session_id(self) -> str:
        return session_id_for(self.client_id, self.project_id, self.user_id)


class AgentService:
    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self.cache: Optional[redis.Redis] = (redis.Redis.from_url(settings.redis_connection_url,
                                                                  decode_responses=True)
                                             if settings.redis_url else None)
        self.repository = PostgresRedisSessionRepository(settings.sync_database_url, self.cache,
                                                         settings.memory_ttl_seconds, settings.app_env)
        self._registry = self._build_registry_source(settings)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _build_registry_source(self, settings: AgentSettings) -> Optional[RegistrySkillSource]:
        """The registry-backed skill source, or None when this deployment runs packaged skills only.

        Needs both an address and a service-client secret: without a secret we cannot get a service token for
        registry-service, so there is nothing to fall back from.
        """
        if not settings.registry_mcp_url or not settings.service_client_secret:
            return None
        target = build_skill_registry_target(settings.registry_mcp_url, settings.registry_timeout_s)
        return RegistrySkillSource(target, build_service_token_provider(settings), self.cache, settings.app_env,
                                   max_entries=settings.registry_cache_entries)
        self._running: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ validation / registry
    def _validate(self, tenant: Tenant, request_id: str, **texts: tuple[str, int]) -> None:
        ids_ok = all(0 < v < 2**31 for v in (tenant.client_id, tenant.project_id, tenant.user_id))
        texts_ok = all(value.strip() and len(value) <= limit for value, limit in texts.values())
        if not ids_ok or not texts_ok or not _REQUEST_ID.match(request_id):
            raise AppError(AgentErrors.INPUT_INVALID)

    def _register(self, request_id: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._running[request_id] = task

    def cancel(self, request_id: str) -> bool:
        """Cancel a running request. Returns False if nothing with that id is running."""
        task = self._running.get(request_id)
        if task is None or task.done():
            log_event(log, "agent.cancel_not_running", agent_request_id=request_id)
            return False
        task.cancel()
        metrics.cancellations_total.add(1)
        log_event(log, "agent.request_cancelled", agent_request_id=request_id)
        return True

    def _packaged_skill(self, version: str):
        s = self.settings
        return load_skill(s.default_skill_name, version or s.default_skill_version, s.default_skill_name,
                          s.default_skill_version)

    async def _skill(self, tenant: Tenant, version: str):
        """The skill version to run: the client's activated version from the registry, else the packaged module.

        With no pin, the registry's active version wins and the packaged module is the fallback. A pin that
        looks like a version number ("3" or "v3") is tried in the registry first and falls back to the
        packaged module of the same name, so an existing `SKILL_VERSION=v1` keeps working: it resolves to the
        authored version 1 when the client has one, and to `skills/ats_screening/v1.py` otherwise.
        """
        if self._registry is not None:
            pinned = _version_number(version)
            if pinned is not None or not version:
                bundle = await self._registry.load(tenant.client_id, self.settings.skill_domain,
                                                   self.settings.default_skill_name, pinned)
                if bundle is not None:
                    return bundle
        return self._packaged_skill(version)

    # ------------------------------------------------------------------ use cases
    async def screen(self, tenant: Tenant, request_id: str, chat_id: str, job_description: str, resume_text: str,
                     skill_version: str = "", progress: Optional[ProgressFn] = None) -> ScreeningResult:
        """Run the screening skill autonomously; steps the agent skips are run directly afterwards (and reported)."""
        self._validate(tenant, request_id, jd=(job_description, self.settings.max_jd_chars),
                       resume=(resume_text, self.settings.max_resume_chars))
        bind(agent_request_id=request_id, chat_id=chat_id)
        self._register(request_id)
        outcome = "ok"
        try:
            async with self._locks[tenant.session_id]:
                return await self._screen(tenant, request_id, chat_id, job_description, resume_text,
                                          skill_version, progress)
        except asyncio.CancelledError:
            outcome = "cancelled"
            log_event(log, "agent.screening_cancelled", agent_request_id=request_id)
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self._running.pop(request_id, None)
            metrics.screenings_total.add(1, {"outcome": outcome})

    async def _screen(self, tenant: Tenant, request_id: str, chat_id: str, job_description: str, resume_text: str,
                      skill_version: str, progress: Optional[ProgressFn]) -> ScreeningResult:
        skill = await self._skill(tenant, skill_version)
        model = build_model(tenant.client_id)
        runtime = StepRuntime(settings=self.settings, model=model,
                              model_name=model_identity(Purpose.SCREENING, tenant.client_id), skill=skill,
                              cache=self.cache, progress=progress)
        agent, session_manager = build_agent(self.settings, tenant.session_id, skill, self.repository, model)
        agent.state.set(WORKSPACE, {"request_id": request_id, "chat_id": chat_id, "jd_text": job_description,
                                    "resume_text": resume_text, "corrections": [], "steps_by_agent": []})
        log_event(log, "agent.screening_started", skill=skill.cache_id)
        if progress:
            await progress("agent", f"Agent started (skill {skill.name}/{skill.version})")

        final_message, agent_error = "", ""
        try:
            result = await agent.invoke_async(
                f"New screening request {request_id}. The job description and resume are loaded in your workspace. "
                f"Activate the ats-screening skill and follow its workflow.",
                invocation_state={"runtime": runtime},
            )
            final_message = str(result).strip()[:500]
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the deterministic fallback below still produces a result
            agent_error = type(e).__name__
            if is_llm_format_error(e):   # known LM Studio parse failure: no traceback needed
                log_event(log, "agent.model_format_error", logging.WARNING, attempt=1, operation="screen")
            else:
                log.exception("agent.loop_failed")

        workspace = agent.state.get(WORKSPACE) or {}
        fallback: list[str] = []
        for step in STEPS:
            if not workspace.get(OUTPUT_KEY[step]):
                if progress:
                    await progress(step, f"Agent skipped {step}; running it directly")
                await run_step(step, StepContext(workspace=workspace, runtime=runtime), "fallback")
                fallback.append(step)

        result = ScreeningResult.model_validate(workspace["result"])
        result.agent_trace = {"requestId": request_id, "skill": skill.cache_id,
                              "stepsByAgent": workspace.get("steps_by_agent", []), "stepsFallback": fallback,
                              "corrections": workspace.get("corrections", []), "agentMessage": final_message,
                              "agentError": agent_error}

        history = agent.state.get(HISTORY) or []
        history.append({"date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "chatId": chat_id,
                        "role": result.role_title, "score": result.score, "band": result.band,
                        "recommendation": result.recommendation,
                        "gaps": [r.name for r in result.requirements
                                 if r.grade in ("absent", "invalidated", "adjacent")][:8]})
        agent.state.set(HISTORY, history[-self.settings.history_entries:])
        agent.state.set(WORKSPACE, {"request_id": request_id, "chat_id": chat_id})  # drop large texts
        session_manager.sync_agent(agent)
        log_event(log, "agent.screening_completed", score=result.score, fallback_steps=len(fallback))
        return result

    @retry(MODEL_FORMAT_RETRY, op="agent.ask")
    async def _ask_once(self, tenant: Tenant, question: str) -> str:
        """One agent run (fresh model + agent per attempt, so a retry never reuses a broken loop)."""
        skill = await self._skill(tenant, "")
        model = build_model(tenant.client_id)
        agent, _ = build_agent(self.settings, tenant.session_id, skill, self.repository, model)
        runtime = StepRuntime(settings=self.settings, model=model,
                              model_name=model_identity(Purpose.SCREENING, tenant.client_id), skill=skill,
                              cache=self.cache)
        result = await agent.invoke_async(question, invocation_state={"runtime": runtime})
        return str(result).strip()

    async def ask(self, tenant: Tenant, request_id: str, question: str) -> str:
        """Free conversation; the agent remembers this user's screenings in this project."""
        self._validate(tenant, request_id, question=(question, self.settings.max_question_chars))
        bind(agent_request_id=request_id)
        self._register(request_id)
        try:
            async with self._locks[tenant.session_id]:
                try:
                    return await self._ask_once(tenant, question)
                except Exception as e:
                    if is_llm_format_error(e):
                        raise AppError(Errors.LLM_MALFORMED) from None
                    raise
        finally:
            self._running.pop(request_id, None)


def new_request_id() -> str:
    return uuid.uuid4().hex
