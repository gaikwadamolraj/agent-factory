"""MCP server for the compute agent (streamable HTTP at /mcp). Entry point: `uvicorn ats_compute.server:app`.

Request pipeline (outermost first): OTEL span -> request context -> security headers -> service token
(zero trust, aud=ats-compute) -> tool allow-list -> MCP. Tool arguments and results use camelCase.
"""
import asyncio
import atexit
import json
from functools import lru_cache

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from ats_common.constants import JWT_AUDIENCE_COMPUTE
from ats_common.errors import AppError, Errors
from ats_common.logging import get_logger
from ats_common.middleware import (PathRules, RequestContextMiddleware, SecurityHeadersMiddleware,
                                   ServiceAuthMiddleware, ToolPolicyMiddleware)
from ats_common.telemetry import instrument_asgi, setup_observability
from ats_compute.config import get_settings
from ats_compute.errors import ComputeErrors
from ats_compute.service import ComputeService

settings = get_settings()
observability = setup_observability(settings)
atexit.register(observability.shutdown)
log = get_logger("ats_compute")
MCP_PATH = "/mcp"

mcp = MCPServer(
    name="ats-compute",
    instructions="Compute agent. execute_code runs a Python program written by the caller in an isolated sandbox and "
                 "returns its output as text; cancel_request stops a running execution.",
    debug=not settings.is_prod,
)


@lru_cache
def get_service() -> ComputeService:
    return ComputeService(settings)


async def _safely(coro):
    """Tool errors carry only a catalog code + message; never internal details or stack traces."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except AppError as e:
        raise RuntimeError(f"{e.spec.code}: {e.spec.message} {e.spec.hint}") from None
    except Exception:
        log.exception("compute.tool_unhandled_error")
        raise RuntimeError(f"{Errors.INTERNAL.code}: {Errors.INTERNAL.message} {Errors.INTERNAL.hint}") from None


@mcp.tool()
async def execute_code(requestId: str, code: str, language: str = "python", timeoutS: float = 0) -> str:  # noqa: N803
    """Run a complete program in an isolated sandbox (no network, no files kept, standard library only) and return
    the outcome as text: "exit code: N", then stdout, then stderr if any. JSON printed by the program is returned
    as text inside stdout.

    Args:
        requestId: Caller-generated id (6-64 chars [A-Za-z0-9_-]); use it with cancel_request to abort.
        code: The full program source. It must print its result.
        language: Only "python" is supported.
        timeoutS: Wall-time limit in seconds (0 = default; capped by the server).
    """
    return await _safely(get_service().execute(requestId, language, code, timeoutS or None))


@mcp.tool()
async def cancel_request(requestId: str) -> str:  # noqa: N803
    """Cancel a running compute request (stops the LLM call and the loop). Returns {"cancelled": bool}.

    Args:
        requestId: The id passed to the request that should stop.
    """
    return json.dumps({"cancelled": get_service().cancel(requestId)})


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


_mcp_app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # ClusterIP only
)
_mcp_rules = PathRules(protected=(MCP_PATH,))
_secured = ToolPolicyMiddleware(_mcp_app, _mcp_rules, settings.tool_policy_map())
_secured = ServiceAuthMiddleware(_secured, settings.require_jwt_secret(), JWT_AUDIENCE_COMPUTE, _mcp_rules,
                                 settings.caller_set(), settings.jwt_algorithm)
_secured = SecurityHeadersMiddleware(_secured)
_secured = RequestContextMiddleware(_secured, settings.service_name, expose_served_by=not settings.is_prod)
app = instrument_asgi(_secured) if settings.otel_enabled else _secured

_ = ComputeErrors  # register compute error codes at import


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
