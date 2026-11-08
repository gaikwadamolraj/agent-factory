"""The MCP front door: every tool in the catalog, registered on one MCPServer, each a thin wrapper over the shared
pipeline (dispatch.invoke_tool). The MCP server is mounted behind the repo's secure wrapper (service token for aud
`registry-service` + ALLOWED_CALLERS + per-tool TOOL_POLICY), so a service/agent reaches exactly the same operations as
the REST wrapper, authorized by the same code (docs/impl_registry_service.md section 3).

A tool arriving over MCP is always a service caller (the service token's `sub`, put on the request context by the
ServiceAuthMiddleware); the pipeline authorizes it with the machine TOOL_POLICY layer. No agent drives registry-service
yet, so ALLOWED_CALLERS/TOOL_POLICY start empty (default deny).
"""
import asyncio
import inspect
import json

from mcp.server.mcpserver import MCPServer

from agent_common.context import current_context
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger

from registry_service import catalog
from registry_service.config import FactorySettings
from registry_service.container import active_container
from registry_service.dispatch import invoke_tool
from registry_service.principals import ServicePrincipal

log = get_logger("registry_service.mcp")


def _tool_error(error: Exception) -> RuntimeError:
    """MCP tool errors carry only a catalog code + message (never internal details or a stack trace)."""
    spec = error.spec if isinstance(error, AppError) else Errors.INTERNAL
    return RuntimeError(f"{spec.code}: {spec.message} {spec.hint}")


def _make_mcp_tool(spec):
    """Build an MCP tool function for one ToolSpec: a single typed request parameter (so MCP derives the schema) that
    delegates to the shared pipeline as the verified service caller."""
    model = spec.request_model

    async def _tool(request) -> str:          # `request` is annotated with the spec's model below
        caller = current_context().caller_service or ""
        principal = ServicePrincipal(caller)
        try:
            result = await invoke_tool(active_container(), principal, spec.name,
                                       request.model_dump(by_alias=False), front_door="mcp")
        except asyncio.CancelledError:
            raise
        except AppError as e:
            raise _tool_error(e) from None
        except Exception as e:                # unexpected: hide internals
            log.exception("registry_service.mcp_tool_error")
            raise _tool_error(e) from None
        return json.dumps(result)

    # Make both inspect.signature and typing.get_type_hints see the typed request parameter.
    _tool.__name__ = spec.name
    _tool.__qualname__ = spec.name
    _tool.__doc__ = spec.description
    _tool.__annotations__ = {"request": model, "return": str}
    _tool.__signature__ = inspect.Signature(
        [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=model)],
        return_annotation=str)
    return _tool


def build_mcp_server(settings: FactorySettings) -> MCPServer:
    """Create the MCPServer and register every catalog tool on it."""
    mcp = MCPServer(
        name="registry-service",
        instructions="Admin & authoring plane: skills and versions, approvals, IAM policies and groups, delegated "
                     "user/client/project administration, notifications and a tool-catalogue proxy. Every tool is "
                     "also reachable at POST /api/v1/factory/{tool}.",
        debug=not settings.is_prod,
    )
    for spec in catalog.all_specs():
        mcp.add_tool(_make_mcp_tool(spec), name=spec.name, description=spec.description)
    return mcp
