"""Versioned skills: ats_agent/skills/<name>/<version>.py.

A skill version defines SKILL_MD (a Strands Agent Skill: frontmatter + workflow instructions the agent activates on
demand) and the step prompts its tools use. An unknown version falls back to the default version.
"""
import hashlib
import importlib

import re
from dataclasses import dataclass
from functools import lru_cache

from strands.vended_plugins.skills import Skill

from ats_common.logging import get_logger

log = get_logger(__name__)

STEP_PROMPTS = ("JD_ANALYSIS", "PROFILE_ANALYSIS", "MATCH", "VERDICT")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


@dataclass(frozen=True)
class SkillBundle:
    name: str
    version: str
    skill: Skill
    prompts: dict[str, str]

    @property
    def cache_id(self) -> str:
        """name@version:hash. Part of cache keys, so a changed skill never serves stale results."""
        content = "\n".join([self.skill.instructions, *(self.prompts[k] for k in STEP_PROMPTS)])
        return f"{self.name}@{self.version}:{hashlib.sha256(content.encode()).hexdigest()[:10]}"

    def render(self, prompt: str, **values: str) -> str:
        # Single pass: placeholder-like text inside JD/resume values is never substituted again.
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), self.prompts[prompt])


def _import(name: str, version: str):
    if not (_IDENTIFIER.match(name) and _IDENTIFIER.match(version)):
        return None
    try:
        module = importlib.import_module(f"{__name__}.{name}.{version}")
    except ModuleNotFoundError:
        return None
    if not isinstance(getattr(module, "SKILL_MD", None), str) or not all(
        isinstance(getattr(module, p, None), str) for p in STEP_PROMPTS
    ):
        log.warning("Skill %s/%s is invalid (needs SKILL_MD + %s)", name, version, ", ".join(STEP_PROMPTS))
        return None
    return module


@lru_cache
def load_skill(name: str, version: str, default_name: str, default_version: str) -> SkillBundle:
    module = _import(name, version)
    if module is None:
        log.warning("Skill %s/%s not available, using default %s/%s", name, version, default_name, default_version)
        name, version = default_name, default_version
        module = _import(name, version)
        if module is None:
            raise RuntimeError(f"Default skill {name}/{version} is missing or invalid.")
    return SkillBundle(
        name=name,
        version=version,
        skill=Skill.from_content(module.SKILL_MD),
        prompts={p: getattr(module, p) for p in STEP_PROMPTS},
    )
