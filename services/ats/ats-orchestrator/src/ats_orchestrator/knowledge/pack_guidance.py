"""Knowledge guidance and ontology from the domain pack, with the packaged module as fallback.

Today the ATS guidance strings and `talent_v2.json` are module constants inside this service. In the pack
model they are files on a published version (docs/impl_multi_domain.md section 6), so a client can change what
is extracted - and which ontology shapes the graph - without a release.

**Why this module is careful.** Knowledge ingest is fire-and-forget: a bad guidance string does not raise, it
quietly produces a worse graph, and the only trace is a log line. So the rule here is all-or-nothing: a pack
supplies the guidance *and* the ontology, or it supplies neither and the packaged module is used unchanged.
Half a pack is worse than no pack, because empty guidance still "works".
"""
from dataclasses import dataclass
from typing import Optional

from agent_common.authz import read_skill_version
from agent_common.knowledge.ontology import parse_ontology
from agent_common.logging import get_logger, log_event
from agent_common.pack.source import RegistryPackSource, ResolvedPack

from ats_orchestrator.knowledge.guidance import TalentGuidance, load_guidance, load_ontology

log = get_logger(__name__)

# Paths inside a pack version. The ingest/answer guidance paths are declared in pack.json; these are the
# defaults used when the document does not name them.
DEFAULT_INGEST_PATH = "guidance/ingest.md"
DEFAULT_ANSWER_PATH = "guidance/answer.md"
SCREENING_GUIDANCE_PATH = "guidance/screening_ingest.md"


@dataclass(frozen=True)
class ResolvedGuidance:
    """What the knowledge calls need, whatever the source: the same shape as a packaged TalentGuidance."""

    version: str
    profile_extraction: str
    screening_extraction: str
    answering: str
    ontology: Optional[dict]
    source: str              # "pack" or "module" - which one actually supplied this, for logs and support


def from_module(guidance: TalentGuidance) -> ResolvedGuidance:
    """The packaged guidance and ontology this service ships with."""
    return ResolvedGuidance(version=guidance.version, profile_extraction=guidance.profile_extraction,
                            screening_extraction=guidance.screening_extraction, answering=guidance.answering,
                            ontology=load_ontology(guidance.profile_ontology), source="module")


def from_pack(pack: ResolvedPack, fallback: TalentGuidance) -> Optional[ResolvedGuidance]:
    """Guidance and ontology from a resolved pack, or None when the pack cannot supply a complete set.

    Returns None - meaning "use the module" - when the version carries no usable pack document, when a
    declared file is missing or empty, or when the ontology does not parse. Each case is logged with the
    reason, because a silently degraded graph is the failure mode this module exists to prevent.
    """
    document = pack.document
    if document is None:
        log_event(log, "knowledge.pack_absent", version=pack.version_number, reason="no pack.json")
        return None

    ingest_path = document.guidance.ingest_path or DEFAULT_INGEST_PATH
    answer_path = document.guidance.answer_path or DEFAULT_ANSWER_PATH
    ontology_path = document.ontology.path if document.ontology is not None else ""
    if not ontology_path:
        log_event(log, "knowledge.pack_incomplete", version=pack.version_number, reason="no ontology declared")
        return None
    if not pack.has_all(ingest_path, answer_path, ontology_path):
        missing = [path for path in (ingest_path, answer_path, ontology_path) if not pack.files.get(path)]
        log_event(log, "knowledge.pack_incomplete", version=pack.version_number, missing=",".join(missing))
        return None

    raw_ontology = pack.json(ontology_path)
    if raw_ontology is None:
        log_event(log, "knowledge.pack_incomplete", version=pack.version_number, reason="ontology is not JSON")
        return None
    try:
        parse_ontology(raw_ontology)            # validate here so a bad ontology fails loudly, not at ingest
    except Exception as error:
        log_event(log, "knowledge.pack_ontology_invalid", version=pack.version_number,
                  error_type=type(error).__name__)
        return None

    # Screening guidance is optional in a pack: it is a small, stable string, so the packaged one is a safe
    # default rather than a reason to reject the whole pack.
    screening = pack.text(SCREENING_GUIDANCE_PATH) or fallback.screening_extraction
    resolved = ResolvedGuidance(version=f"pack:v{pack.version_number}",
                                profile_extraction=pack.text(ingest_path),
                                screening_extraction=screening,
                                answering=pack.text(answer_path),
                                ontology=raw_ontology, source="pack")
    log_event(log, "knowledge.pack_applied", version=pack.version_number, namespace=document.ontology.namespace,
              cache_id=pack.cache_id)
    return resolved


class GuidanceResolver:
    """Per-client guidance: the client's activated pack when it has a complete one, else the packaged module.

    Resolution is per request and cached inside RegistryPackSource (invalidated by the skill-version counter),
    so activating a new pack changes extraction without a restart.
    """

    def __init__(self, source: Optional[RegistryPackSource], domain: str, skill_name: str,
                 fallback: TalentGuidance) -> None:
        self._source, self._domain, self._skill_name = source, domain, skill_name
        self._fallback = fallback
        self._module = from_module(fallback)

    @property
    def module_guidance(self) -> ResolvedGuidance:
        """The packaged guidance, used when no pack applies (and by callers that cannot await)."""
        return self._module

    async def for_client(self, client_id: int) -> ResolvedGuidance:
        """Never raises: any problem resolving or applying a pack falls back to the packaged guidance."""
        if self._source is None:
            return self._module
        try:
            pack = await self._source.resolve(client_id, self._domain, self._skill_name)
        except Exception as error:               # the source already swallows its own failures; belt and braces
            log_event(log, "knowledge.pack_resolve_failed", error_type=type(error).__name__)
            return self._module
        if pack is None:
            return self._module
        return from_pack(pack, self._fallback) or self._module


def build_resolver(settings, tokens, redis, fallback_version: str = "") -> GuidanceResolver:
    """Wire a resolver from settings.

    With no registry address, no service-token provider or no Redis, it always yields the packaged guidance:
    the skill-version counter is what invalidates a cached pack, so without it we would happily serve a
    superseded pack forever - worse than not using packs at all.
    """
    from agent_common.pack.source import build_registry_target

    from ats_orchestrator.errors import OrchErrors

    fallback = load_guidance(fallback_version or settings.knowledge_guidance_version)
    if not settings.registry_mcp_url or tokens is None or redis is None:
        return GuidanceResolver(None, settings.pack_domain, settings.pack_skill_name, fallback)

    async def read_counter() -> int:
        return await read_skill_version(redis, settings.app_env)

    target = build_registry_target(settings.registry_mcp_url, settings.registry_timeout_s,
                                   OrchErrors.REGISTRY_UNAVAILABLE, OrchErrors.REGISTRY_TIMEOUT)
    return GuidanceResolver(RegistryPackSource(target, tokens, read_counter),
                            settings.pack_domain, settings.pack_skill_name, fallback)
