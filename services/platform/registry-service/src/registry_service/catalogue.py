"""Tool-catalogue proxy: browse and (within policy) invoke tools on OTHER registered MCP servers.

The set of servers comes from config today (a static map), behind a small interface so a DB-backed registry can
replace it later without touching callers. The catalogue is cached in-process and refreshable (a version counter);
invocation reuses agent_common.mcp_client (service token for the target + resilience). The auth+policy pipeline that
guards a proxied call is the same one used for registry-service's own tools - the action is `tool:{server}.{tool}`.
"""
from dataclasses import dataclass

from agent_common.logging import get_logger, log_event
from agent_common.mcp_client import McpTarget, McpToolClient
from agent_common.security import ServiceTokenProvider

from registry_service.errors import FactoryErrors
from agent_common.errors import AppError

log = get_logger(__name__)


@dataclass(frozen=True)
class RegisteredServer:
    """A registered MCP server the catalogue can reach (name + base URL + the JWT audience it accepts)."""

    name: str
    base_url: str
    audience: str


class ToolCatalogue:
    """In-memory, refreshable registry of MCP servers + a client factory for invoking their tools.

    `version` is bumped by `refresh()`, so the admin UI can tell when the catalogue changed. The map is static (from
    config) now; swap `_load` for a DB-backed loader later and nothing else changes.
    """

    def __init__(self, registry: dict[str, tuple[str, str]], tokens: ServiceTokenProvider,
                 unavailable=None, timeout=None, call_timeout_s: float = 30.0):
        self._registry_config = registry
        self._tokens = tokens
        self._call_timeout_s = call_timeout_s
        self._unavailable = unavailable or FactoryErrors.UNKNOWN_MCP_SERVER
        self._timeout = timeout or FactoryErrors.UNKNOWN_MCP_SERVER
        self._servers: dict[str, RegisteredServer] = {}
        self.version = 0
        self.refresh()

    def refresh(self) -> int:
        """Reload the server map and bump the catalogue version. Returns the new version."""
        self._servers = {name: RegisteredServer(name, base_url, audience)
                         for name, (base_url, audience) in self._registry_config.items()}
        self.version += 1
        log_event(log, "catalogue.refreshed", servers=len(self._servers), version=self.version)
        return self.version

    def list_servers(self) -> list[dict]:
        """The registered servers (name + audience; never a URL secret). Low-risk metadata for the admin UI."""
        return [{"name": s.name, "audience": s.audience} for s in self._servers.values()]

    def get(self, server: str) -> RegisteredServer:
        """One registered server, or AGT-NFD-0001 when it is not registered."""
        server_def = self._servers.get(server)
        if server_def is None:
            raise AppError(FactoryErrors.UNKNOWN_MCP_SERVER)
        return server_def

    async def invoke(self, server: str, tool: str, arguments: dict) -> str:
        """Call `tool` on a registered `server` with a service token for its audience; returns the tool's text result."""
        server_def = self.get(server)
        target = McpTarget(name=f"catalogue.{server}", url=f"{server_def.base_url.rstrip('/')}/mcp",
                           audience=server_def.audience, timeout_s=self._call_timeout_s,
                           unavailable=self._unavailable, timeout=self._timeout)
        client = McpToolClient(target, self._tokens)
        return await client.call(tool, arguments)
