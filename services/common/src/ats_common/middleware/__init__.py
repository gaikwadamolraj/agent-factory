"""Pure-ASGI middlewares (safe for streaming/SSE and for MCP servers).

Recommended order (outermost first) - Starlette's `add_middleware` adds outermost LAST, so register in reverse:

    1. RequestContextMiddleware   request id, context vars, access log, X-Request-Id / X-Served-By
    2. SecurityHeadersMiddleware  nosniff, frame-deny, no-store on APIs
    2b. BodySizeLimitMiddleware   413 before an oversized upload is read
    3. CORS (framework)           preflight answered before auth
    4. JWTAuthMiddleware          user token  (APIs)        | ServiceAuthMiddleware  service token (MCP)
    5. TenantMiddleware           X-Client-Id/X-Project-Id  | ToolPolicyMiddleware   allowed tools per caller
"""
from ats_common.middleware.auth import JWTAuthMiddleware, ServiceAuthMiddleware, cookie
from ats_common.middleware.body_limit import BodySizeLimitMiddleware
from ats_common.middleware.context import RequestContextMiddleware, SecurityHeadersMiddleware
from ats_common.middleware.paths import PathRules
from ats_common.middleware.tenant import TenantMiddleware
from ats_common.middleware.tool_policy import ToolPolicyMiddleware

__all__ = ["BodySizeLimitMiddleware", "JWTAuthMiddleware", "cookie", "PathRules", "RequestContextMiddleware", "SecurityHeadersMiddleware",
           "ServiceAuthMiddleware", "TenantMiddleware", "ToolPolicyMiddleware"]
