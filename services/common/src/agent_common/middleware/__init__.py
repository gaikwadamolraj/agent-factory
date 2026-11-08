"""Pure-ASGI middlewares (safe for streaming/SSE and for MCP servers).

Recommended order (outermost first) - Starlette's `add_middleware` adds outermost LAST, so register in reverse:

    1. RequestContextMiddleware   request id, context vars, access log, X-Request-Id / X-Served-By
    2. SecurityHeadersMiddleware  nosniff, frame-deny, no-store on APIs
    2b. BodySizeLimitMiddleware   413 before an oversized upload is read
    3. CORS (framework)           preflight answered before auth
    4. JWTAuthMiddleware          user token  (APIs)        | ServiceAuthMiddleware  service token (MCP)
    5. TenantMiddleware           X-Client-Id/X-Project-Id  | ToolPolicyMiddleware   allowed tools per caller

Tokens are verified with the auth-service public keys (agent_common.security.JwksVerifier). MCP servers get steps
1, 2, 4 and 5 in one call: `secure_mcp_app(...)`.
"""
from agent_common.middleware.auth import JWTAuthMiddleware, ServiceAuthMiddleware, bearer_token, cookie
from agent_common.middleware.body_limit import BodySizeLimitMiddleware
from agent_common.middleware.context import RequestContextMiddleware, SecurityHeadersMiddleware
from agent_common.middleware.mcp import MCP_PATH, secure_mcp_app
from agent_common.middleware.paths import PathRules
from agent_common.middleware.tenant import TenantMiddleware
from agent_common.middleware.tool_policy import ToolPolicyMiddleware

__all__ = ["BodySizeLimitMiddleware", "JWTAuthMiddleware", "MCP_PATH", "PathRules", "RequestContextMiddleware",
           "SecurityHeadersMiddleware", "ServiceAuthMiddleware", "TenantMiddleware", "ToolPolicyMiddleware",
           "bearer_token", "cookie", "secure_mcp_app"]
