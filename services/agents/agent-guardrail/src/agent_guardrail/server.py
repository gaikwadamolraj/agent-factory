"""MCP server of the guardrails agent (streamable HTTP at /mcp). Entry point:
`uvicorn agent_guardrail.server:app`.

Any service can use it: add the caller to ALLOWED_CALLERS and `check_text` to its TOOL_POLICY entry, then call it
through `agent_common.guardrails.client.GuardrailsClient`. Request pipeline (outermost first): request context ->
security headers -> service token (zero trust) -> tool allow-list -> MCP.
The model is loaded and self-tested before the server accepts requests; a failed self-test stops the pod.
"""
import asyncio
import atexit
import time
from contextlib import asynccontextmanager

from agent_common.constants import JWT_AUDIENCE_GUARDRAIL
from agent_common.errors import Errors
from agent_common.guardrails.contracts import CheckRequest
from agent_common.logging import get_logger, log_event
from agent_common.middleware import secure_mcp_app
from agent_common.telemetry import get_meter, instrument_asgi, setup_observability
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_guardrail.classifier import InjectionClassifier
from agent_guardrail.config import get_settings
from agent_guardrail.engine import GuardrailsEngine

settings = get_settings()
observability = setup_observability(settings)
atexit.register(observability.shutdown)
log = get_logger("agent_guardrail")
MCP_PATH = "/mcp"
_meter = get_meter("agent_guardrail")
checks_total = _meter.create_counter("agent_guardrails_checks_total", description="Guardrail checks by source/outcome")
check_duration = _meter.create_histogram("agent_guardrails_check_duration_seconds", unit="s",
                                         description="Time to check one text")
_engine: GuardrailsEngine | None = None

mcp = MCPServer(
    name="agent-guardrail",
    instructions="Guardrails for untrusted text. check_text says whether a text (document, chat message) tries to "
                 "instruct an LLM and must be blocked, with the classifier score and pattern signals.",
    debug=not settings.is_prod,
)


def build_engine() -> GuardrailsEngine:
    """Load + self-test the classifier, then build the NeMo engine (blocking; runs once at start-up)."""
    classifier = InjectionClassifier(settings)
    classifier.load()
    classifier.self_test()
    return GuardrailsEngine(settings, classifier)


@mcp.tool()
async def check_text(request: CheckRequest) -> str:
    """Should this untrusted text be blocked before it reaches an LLM? Returns {blocked, score, signals, reasons,
    detector}.

    Args:
        request: text (up to 200,000 characters) and source (chat_message, resume, job_description, document, other).
    """
    started = time.perf_counter()
    try:
        result = await _engine.check(request.text[: settings.max_text_chars])
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("guardrails.check_failed")
        spec = Errors.INTERNAL
        raise RuntimeError(f"{spec.code}: {spec.message} {spec.hint}") from None
    outcome = "blocked" if result.blocked else "allowed"
    checks_total.add(1, {"source": request.source.value, "outcome": outcome})
    check_duration.record(time.perf_counter() - started, {"source": request.source.value})
    if result.blocked:
        log_event(log, "guardrails.blocked", source=request.source.value, score=result.score,
                  reasons=result.reasons)      # never the text itself
    return result.model_dump_json(by_alias=True)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    ready = _engine is not None
    return JSONResponse({"status": "ok" if ready else "loading"}, status_code=200 if ready else 503)


_mcp_app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # ClusterIP only
)


_mcp_lifespan = _mcp_app.router.lifespan_context      # the MCP session manager's own start-up


@asynccontextmanager
async def lifespan(app):
    """Load the model before serving; keep the MCP app's own lifespan (session manager) running."""
    global _engine
    _engine = await asyncio.to_thread(build_engine)
    async with _mcp_lifespan(app):
        yield


_mcp_app.router.lifespan_context = lifespan
# request context -> security headers -> service token (JWKS) -> tool allow-list -> MCP
_secured = secure_mcp_app(_mcp_app, settings, JWT_AUDIENCE_GUARDRAIL, MCP_PATH)
app = instrument_asgi(_secured) if settings.otel_enabled else _secured
