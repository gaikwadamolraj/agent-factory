"""The standard middleware stack of an internal MCP server (agent-skill, agent-compute, ...)."""
from starlette.types import ASGIApp

from agent_common.config import McpServiceSettings
from agent_common.middleware.auth import ServiceAuthMiddleware
from agent_common.middleware.context import RequestContextMiddleware, SecurityHeadersMiddleware
from agent_common.middleware.paths import PathRules
from agent_common.middleware.tool_policy import ToolPolicyMiddleware
from agent_common.security import JwksVerifier, build_jwks_verifier

MCP_PATH = "/mcp"


def secure_mcp_app(mcp_app: ASGIApp, settings: McpServiceSettings, audience: str, mcp_path: str = MCP_PATH,
                   verifier: JwksVerifier | None = None) -> ASGIApp:
    """Wrap an MCP ASGI app (outermost first): request context -> security headers -> service token for
    `audience` from ALLOWED_CALLERS (verified with the auth-service JWKS) -> TOOL_POLICY allow-list -> MCP.

    `verifier` defaults to the JWKS verifier from the settings (tests pass one backed by an in-memory key).
    Paths outside `mcp_path` (e.g. /healthz) need no token.
    """
    rules = PathRules(protected=(mcp_path,))
    secured: ASGIApp = ToolPolicyMiddleware(mcp_app, rules, settings.tool_policy_map())
    secured = ServiceAuthMiddleware(secured, verifier or build_jwks_verifier(settings), audience, rules,
                                    settings.caller_set())
    secured = SecurityHeadersMiddleware(secured)
    return RequestContextMiddleware(secured, settings.service_name, expose_served_by=not settings.is_prod)
