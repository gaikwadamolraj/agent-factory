"""Resolve a published pack/skill version from registry-service.

Both the skill runtime (agent-skill) and the product orchestrator need the same thing: the files of the version
a client has activated, cached, and invalidated the moment someone activates a different one. That logic lives
here once rather than in each service (CLAUDE.md section 2).

What it does *not* do is interpret the files: a caller maps them to whatever it needs - a Strands skill bundle,
the knowledge guidance and ontology, a rubric. `ResolvedPack.document` is parsed only when the version actually
carries a `pack.json`.

Failure is always `None`, never an exception: a registry outage must degrade a caller to its built-in defaults,
not fail a user's request. Every failure is logged with its error code.
"""
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Optional

from agent_common.constants import JWT_AUDIENCE_REGISTRY
from agent_common.errors import AppError, ErrorSpec
from agent_common.logging import get_logger, log_event
from agent_common.mcp_client import McpTarget, McpToolClient
from agent_common.pack import PACK_FILE, parse_pack
from agent_common.pack.models import PackDocument
from agent_common.security import ServiceTokenProvider

log = get_logger(__name__)

RESOLVE_TOOL = "resolve_skill"
MCP_PATH = "/mcp"


@dataclass(frozen=True)
class ResolvedPack:
    """One published version: its files by path, and its pack document when it carries one."""

    domain: str
    name: str
    version_number: int
    state: str
    cache_id: str
    files: dict[str, str]
    workflow: dict = field(default_factory=dict)
    document: Optional[PackDocument] = None

    def text(self, path: str) -> str:
        """The content of one file, or "" when the version does not carry it."""
        return self.files.get(path, "")

    def json(self, path: str) -> Optional[dict]:
        """One file parsed as JSON, or None when absent or malformed (malformed is logged, never raised)."""
        raw = self.files.get(path)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            log_event(log, "pack.file_malformed", domain=self.domain, skill=self.name, path=path)
            return None
        return value if isinstance(value, dict) else None

    def has_all(self, *paths: str) -> bool:
        """True when every named file is present and non-empty - the check before trusting a version."""
        return all(self.files.get(path) for path in paths)


@dataclass(frozen=True)
class _Entry:
    counter: int
    pack: ResolvedPack


class RegistryPackSource:
    """Resolves versions from registry-service, cached per (client, domain, name, pin).

    The cache is invalidated by the shared skill-version counter that activate/archive bump, so a new active
    version reaches every replica at once without polling the database and without a TTL that could serve a
    stale version in between.

    `read_counter` is injected because services hold different Redis clients (agent-skill's is synchronous);
    it must never raise - return 0 when the counter cannot be read, which only costs a re-resolution.
    """

    def __init__(self, target: McpTarget, tokens: ServiceTokenProvider,
                 read_counter: Callable[[], Awaitable[int]], max_entries: int = 256) -> None:
        self._client = McpToolClient(target, tokens)
        self._read_counter = read_counter
        self._cache: dict[tuple[int, str, str, str], _Entry] = {}
        self._max_entries = max_entries

    async def resolve(self, client_id: int, domain: str, name: str,
                      version_number: Optional[int] = None) -> Optional[ResolvedPack]:
        """The version this client should run, or None when the registry cannot answer.

        None covers every "use your defaults" case: no such skill, no activated version, registry unreachable,
        or a malformed response. The caller decides what its defaults are.
        """
        counter = await self._counter()
        key = (client_id, domain, name, str(version_number or ""))
        hit = self._cache.get(key)
        if hit is not None and hit.counter == counter:
            return hit.pack

        arguments: dict = {"clientId": client_id, "domain": domain, "name": name}
        if version_number is not None:
            arguments["versionNumber"] = version_number
        try:
            payload = json.loads(await self._client.call(RESOLVE_TOOL, arguments))
        except AppError as error:
            log_event(log, "pack.registry_unavailable", domain=domain, skill=name, code=error.spec.code)
            return None
        except (ValueError, TypeError) as error:
            log_event(log, "pack.registry_malformed", domain=domain, skill=name, error_type=type(error).__name__)
            return None

        pack = self._to_pack(domain, name, payload)
        if pack is None:
            return None
        if len(self._cache) >= self._max_entries:
            self._cache.clear()          # bounded: a cold rebuild is cheap, unbounded growth is not
        self._cache[key] = _Entry(counter=counter, pack=pack)
        log_event(log, "pack.resolved", domain=domain, skill=name, version=pack.version_number,
                  cache_id=pack.cache_id, files=len(pack.files), has_pack=pack.document is not None)
        return pack

    async def _counter(self) -> int:
        try:
            return await self._read_counter()
        except Exception as error:       # cache availability must never fail a request
            log_event(log, "pack.version_counter_unavailable", error_type=type(error).__name__)
            return 0

    @staticmethod
    def _to_pack(domain: str, name: str, payload: dict) -> Optional[ResolvedPack]:
        """Map the tool's response onto ResolvedPack; None when the response is not usable."""
        if not isinstance(payload, dict):
            return None
        files = {entry["path"]: entry.get("content", "")
                 for entry in payload.get("files", []) if isinstance(entry, dict) and entry.get("path")}
        document = None
        if files.get(PACK_FILE):
            try:
                document = parse_pack(files[PACK_FILE])
            except AppError as error:
                # A published version should never hold an invalid pack (the registry validates on write and at
                # submit), so this means something bypassed that path. Use the version without its pack rather
                # than failing, and say so loudly.
                log_event(log, "pack.document_invalid", domain=domain, skill=name, code=error.spec.code)
        try:
            return ResolvedPack(domain=domain, name=name,
                                version_number=int(payload.get("versionNumber") or 0),
                                state=str(payload.get("state") or ""),
                                cache_id=str(payload.get("cacheId") or ""),
                                files=files, workflow=payload.get("workflow") or {}, document=document)
        except (TypeError, ValueError):
            log_event(log, "pack.registry_malformed", domain=domain, skill=name, error_type="shape")
            return None


def build_registry_target(base_url: str, timeout_s: float, unavailable: ErrorSpec,
                          timeout: ErrorSpec) -> McpTarget:
    """The MCP target for registry-service. Short timeout: this is a cached config read on the request path."""
    return McpTarget(name="registry", url=f"{base_url.rstrip('/')}{MCP_PATH}", audience=JWT_AUDIENCE_REGISTRY,
                     timeout_s=timeout_s, unavailable=unavailable, timeout=timeout)
