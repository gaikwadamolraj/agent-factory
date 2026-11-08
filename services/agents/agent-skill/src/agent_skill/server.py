"""MCP server for the screening agent (streamable HTTP at /mcp). Entry point: `uvicorn agent_skill.server:app`.

Request pipeline (outermost first): OTEL span -> request context -> security headers -> service token
(zero trust) -> tool allow-list -> MCP. Tool arguments and results use camelCase.
"""
import asyncio
import atexit
from functools import lru_cache

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_skill.config import get_settings
from agent_skill.errors import AgentErrors
from agent_skill.service import AgentService, Tenant
from agent_common.constants import JWT_AUDIENCE_SKILL
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger
from agent_common.middleware import secure_mcp_app
from agent_common.telemetry import instrument_asgi, setup_observability

settings = get_settings()
observability = setup_observability(settings)
atexit.register(observability.shutdown)
log = get_logger("agent_skill")

STEP_PROGRESS = {"agent": 0, "analyze_job_description": 1, "analyze_candidate_profile": 2,
                 "match_requirements": 3, "write_verdict": 4}
TOTAL_STEPS = 5
MCP_PATH = "/mcp"

mcp = MCPServer(
    name="agent-skill",
    instructions="ATS screening agent. screen_candidate evaluates a resume against a job description; ask_agent "
                 "answers questions about earlier screenings; cancel_request stops a running request.",
    debug=not settings.is_prod,
)


@lru_cache
def get_service() -> AgentService:
    return AgentService(settings)


async def _safely(coro):
    """Tool errors carry only a catalog code + message; never internal details or stack traces."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except AppError as e:
        raise RuntimeError(f"{e.spec.code}: {e.spec.message} {e.spec.hint}") from None
    except Exception:
        log.exception("agent.tool_unhandled_error")
        raise RuntimeError(f"{Errors.INTERNAL.code}: {Errors.INTERNAL.message} {Errors.INTERNAL.hint}") from None


@mcp.tool()
async def screen_candidate(clientId: int, projectId: int, userId: int, requestId: str, chatId: str,  # noqa: N803
                           jobDescription: str, resumeText: str, ctx: Context, skillVersion: str = "") -> str:
    """Screen a resume against a job description. Returns the screening result as camelCase JSON.

    Args:
        clientId: Tenant (client) id.
        projectId: Project id inside the client.
        userId: Recruiter id; memory is kept per client/project/user.
        requestId: Caller-generated id (6-64 chars [A-Za-z0-9_-]); use it with cancel_request to abort.
        chatId: Chat the request belongs to (stored in the screening history).
        jobDescription: Full job description text.
        resumeText: Full resume text (already extracted from the PDF).
        skillVersion: Skill version (e.g. "v1"); empty or unknown uses the default.
    """
    async def progress(step: str, message: str) -> None:
        await ctx.report_progress(STEP_PROGRESS.get(step, 0) + 0.5, TOTAL_STEPS, f"{step}|{message}")

    async def run() -> str:
        result = await get_service().screen(Tenant(clientId, projectId, userId), requestId, chatId, jobDescription,
                                            resumeText, skillVersion, progress)
        await ctx.report_progress(TOTAL_STEPS, TOTAL_STEPS, "done|Screening complete")
        return result.model_dump_json(by_alias=True)

    return await _safely(run())


@mcp.tool()
async def ask_agent(clientId: int, projectId: int, userId: int, requestId: str, question: str) -> str:  # noqa: N803
    """Ask the screening agent a question; it remembers this user's earlier screenings in this project.

    Args:
        clientId: Tenant (client) id.
        projectId: Project id.
        userId: Recruiter id whose memory to use.
        requestId: Caller-generated id, usable with cancel_request.
        question: The question in plain language (max 2000 characters).
    """
    return await _safely(get_service().ask(Tenant(clientId, projectId, userId), requestId, question))


@mcp.tool()
async def cancel_request(requestId: str) -> str:  # noqa: N803
    """Cancel a running screen_candidate/ask_agent request (stops the LLM call). Returns {"cancelled": bool}.

    Args:
        requestId: The id passed to the request that should stop.
    """
    cancelled = get_service().cancel(requestId)
    return '{"cancelled": true}' if cancelled else '{"cancelled": false}'


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


_mcp_app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # ClusterIP only
)
# request context -> security headers -> service token (JWKS) -> tool allow-list -> MCP
_secured = secure_mcp_app(_mcp_app, settings, JWT_AUDIENCE_SKILL, MCP_PATH)
app = instrument_asgi(_secured) if settings.otel_enabled else _secured

_ = AgentErrors  # register agent error codes at import


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
