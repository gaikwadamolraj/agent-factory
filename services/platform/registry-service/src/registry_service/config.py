"""registry-service settings (environment only; secrets come from the k8s Secret at runtime via agent_common.secrets).

Subclasses McpServiceSettings, so it inherits the MCP server knobs (ALLOWED_CALLERS / TOOL_POLICY / host / port)
used by the MCP front door, on top of the common auth/db/redis settings.
"""
from functools import lru_cache

from agent_common.config import McpServiceSettings
from agent_common.constants import JWT_AUDIENCE_AUTH

from registry_service.constants import SERVICE_NAME


class FactorySettings(McpServiceSettings):
    service_name: str = SERVICE_NAME
    port: int = 8005

    # MCP front door: no agent drives registry-service yet, so the caller allow-list starts empty (default deny). Add a
    # caller here (and a TOOL_POLICY entry) when an agent needs to reach the admin plane over MCP.
    allowed_callers: str = ""
    tool_policy: str = ""

    # Hardening (docs/impl_registry_service.md section 3)
    max_request_kb: int = 256                # admin payloads (a skill version's files) are larger than a login
    cors_origins: str = ""                   # comma-separated allow-list; "*" only outside PROD
    rate_limit_per_minute: int = 120         # per-principal request cap (0 disables the app-side hook)

    # Delegated identity: registry-service owns clients/projects/groups/policies/skills; it calls the auth-service for
    # identity writes (users, roles, invites) over a service token with this audience.
    auth_service_base_url: str = "http://auth-service"
    auth_service_audience: str = JWT_AUDIENCE_AUTH

    # Tool-catalogue proxy: the MCP servers registry-service can browse/invoke tools on, as
    # "name=base_url|audience,other=...". Static for now; a DB-backed registry replaces this loader later.
    mcp_registry: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        """Allowed CORS origins; an explicit allow-list always, falling back to '*' only outside PROD."""
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        return origins if (origins or self.is_prod) else ["*"]

    def mcp_registry_map(self) -> dict[str, tuple[str, str]]:
        """Parse MCP_REGISTRY into {server_name: (base_url, audience)}; malformed entries are skipped."""
        registry: dict[str, tuple[str, str]] = {}
        for entry in self.mcp_registry.split(","):
            name, _, rest = entry.partition("=")
            base_url, _, audience = rest.partition("|")
            if name.strip() and base_url.strip() and audience.strip():
                registry[name.strip()] = (base_url.strip(), audience.strip())
        return registry


@lru_cache
def get_settings() -> FactorySettings:
    return FactorySettings()
