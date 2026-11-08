"""Load a skill version from registry-service instead of importing a Python module.

This is the bridge the authoring plane was missing: admin-ui publishes and activates a version as database
rows, and this is what makes an agent actually run it. Without it, activating a version changes nothing at
runtime (docs/impl_domain_platform.md section 10.3).

Resolution, caching and invalidation live in `agent_common.pack.source` (the orchestrator needs the same
thing for its guidance and ontology); this module only maps a resolved version onto a `SkillBundle`.

Layout of a version's files - the pack layout of docs/impl_multi_domain.md section 6:

    skill.md                      -> SKILL_MD      (Strands skill: frontmatter + workflow)
    prompts/jd_analysis.md        -> JD_ANALYSIS
    prompts/profile_analysis.md   -> PROFILE_ANALYSIS
    prompts/match.md              -> MATCH
    prompts/verdict.md            -> VERDICT

A version missing any of them is an authoring mistake, not a runtime error: it is logged and the packaged
module is used, exactly as an invalid module is today.
"""
import asyncio
from typing import Optional

from strands.vended_plugins.skills import Skill

from agent_common.errors import AppError
from agent_common.logging import get_logger, log_event
from agent_common.mcp_client import McpTarget
from agent_common.pack.source import RegistryPackSource, ResolvedPack, build_registry_target
from agent_common.redis_keys import Keys, build_key
from agent_common.rules import RubricDocument, parse_rubric
from agent_common.security import ServiceTokenProvider

from agent_skill.errors import AgentErrors
from agent_skill.skills import SkillBundle

log = get_logger(__name__)

SKILL_FILE = "skill.md"
PROMPT_PATHS = {
    "JD_ANALYSIS": "prompts/jd_analysis.md",
    "PROFILE_ANALYSIS": "prompts/profile_analysis.md",
    "MATCH": "prompts/match.md",
    "VERDICT": "prompts/verdict.md",
}
# Optional: a version without it is scored by the service's packaged rubric, which is how versions authored
# before rubrics existed keep working unchanged.
RUBRIC_PATH = "rubrics/fit.rubric.json"
REQUIRED_FILES = (SKILL_FILE, *PROMPT_PATHS.values())


class RegistrySkillSource:
    """Resolves the skill version a client has activated and maps it onto a runnable bundle."""

    def __init__(self, target: McpTarget, tokens: ServiceTokenProvider, redis, env: str,
                 max_entries: int = 256) -> None:
        self._redis, self._env = redis, env
        self._source = RegistryPackSource(target, tokens, self._counter, max_entries=max_entries)

    async def load(self, client_id: int, domain: str, name: str, version_number: Optional[int] = None
                   ) -> Optional[SkillBundle]:
        """The bundle the registry says to run, or None when the caller should use its packaged default."""
        pack = await self._source.resolve(client_id, domain, name, version_number)
        if pack is None:
            return None
        return self._to_bundle(pack)

    async def _counter(self) -> int:
        """The shared skill-version counter that activate_version bumps; 0 when Redis cannot answer.

        This service holds a *synchronous* Redis client, so the read goes to a worker thread rather than
        blocking the event loop.
        """
        if self._redis is None:
            return 0
        raw = await asyncio.to_thread(self._redis.get, build_key(Keys.SKILL_ACTIVE_VERSION, self._env))
        return int(raw or 0)

    @staticmethod
    def _to_bundle(pack: ResolvedPack) -> Optional[SkillBundle]:
        """Map a resolved version onto a SkillBundle, or None when it is not runnable as a skill."""
        if not pack.has_all(*REQUIRED_FILES):
            missing = [path for path in REQUIRED_FILES if not pack.files.get(path)]
            log_event(log, "skill.registry_incomplete", domain=pack.domain, skill=pack.name,
                      version=pack.version_number, missing=",".join(missing))
            return None
        return SkillBundle(
            name=pack.name,
            version=f"v{pack.version_number}",
            skill=Skill.from_content(pack.text(SKILL_FILE)),
            prompts={step: pack.text(path) for step, path in PROMPT_PATHS.items()},
            rubric=RegistrySkillSource._rubric(pack),
            cache_id_override=pack.cache_id or "",
        )

    @staticmethod
    def _rubric(pack: ResolvedPack) -> Optional[RubricDocument]:
        """The version's scoring rubric, or None to use the service's packaged default.

        A rubric that will not parse is refused *here*, not half-way through a screening: the version falls
        back to the packaged rubric and says so in the log, because scoring a candidate with a rubric nobody
        could read would be worse than scoring them with the one this service shipped.
        """
        raw = pack.files.get(RUBRIC_PATH)
        if not raw:
            return None
        try:
            return parse_rubric(pack.text(RUBRIC_PATH))
        except AppError as error:
            log_event(log, "skill.rubric_invalid", domain=pack.domain, skill=pack.name,
                      version=pack.version_number, code=error.spec.code)
            return None


def build_skill_registry_target(base_url: str, timeout_s: float) -> McpTarget:
    """The MCP target for registry-service, with this service's error codes for its failure modes."""
    return build_registry_target(base_url, timeout_s, AgentErrors.REGISTRY_UNAVAILABLE,
                                 AgentErrors.REGISTRY_TIMEOUT)
