"""MCP server of the document agent (streamable HTTP at /mcp). Entry point: `uvicorn agent_document.server:app`.

Any app that reads documents can use it: add the caller to ALLOWED_CALLERS and the tools it needs to its TOOL_POLICY
entry, allow the OAuth pair in the auth-service, then call the tools over MCP. Request pipeline (outermost first):
request context -> security headers -> service token (zero trust) -> tool allow-list -> MCP. Arguments/results use
camelCase.
"""
import asyncio
import atexit
from functools import lru_cache

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_common.constants import JWT_AUDIENCE_DOCUMENT
from agent_common.errors import AppError, Errors
from agent_common.logging import get_logger
from agent_common.middleware import secure_mcp_app
from agent_common.security import build_service_token_provider
from agent_common.telemetry import instrument_asgi, setup_observability

from agent_document.config import get_settings
from agent_document.errors import DocumentErrors
from agent_document.schemas import ChunkDocumentRequest, ReadDocumentRequest, ScanDocumentRequest
from agent_document.service import DocumentService

settings = get_settings()
observability = setup_observability(settings)
atexit.register(observability.shutdown)
log = get_logger("agent_document")
MCP_PATH = "/mcp"

mcp = MCPServer(
    name="agent-document",
    instructions="Document reader. read_document extracts text, tables and metadata from a PDF/DOCX/XLSX upload "
                 "(and can injection-scan it); scan_document scans arbitrary text; chunk_document splits text into "
                 "overlapping chunks and can embed them for search.",
    debug=not settings.is_prod,
)


@lru_cache
def get_service() -> DocumentService:
    """One service instance per process. Service tokens are only built when a guardrail URL is configured."""
    tokens = build_service_token_provider(settings) if settings.guardrail_mcp_url else None
    return DocumentService(settings, tokens)


async def _safely(coro):
    """Tool errors carry only a catalog code + message; never internal details or stack traces."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except AppError as e:
        raise RuntimeError(f"{e.spec.code}: {e.detail or e.spec.message} {e.spec.hint}") from None
    except Exception:
        log.exception("document.tool_unhandled_error")
        raise RuntimeError(f"{Errors.INTERNAL.code}: {Errors.INTERNAL.message} {Errors.INTERNAL.hint}") from None


@mcp.tool()
async def read_document(request: ReadDocumentRequest) -> str:
    """Extract text, tables and metadata from an uploaded document. Returns camelCase JSON.

    The format (pdf/docx/xlsx) is detected from the file bytes, not the name or declared type. Set `scan` to also
    injection-scan the extracted text and include a `safety` block.

    Args:
        request: filename, contentType, dataBase64 (the file, base64-encoded), and the optional `scan` flag.
    """
    async def run() -> str:
        result = await get_service().read(request)
        return result.model_dump_json(by_alias=True)

    return await _safely(run())


@mcp.tool()
async def scan_document(request: ScanDocumentRequest) -> str:
    """Injection-scan a block of text through the shared guardrails. Returns {blocked, score, reasons, detector}.

    Args:
        request: text to scan and its source (document, resume, job_description, chat_message, other).
    """
    async def run() -> str:
        result = await get_service().scan(request)
        return result.model_dump_json(by_alias=True)

    return await _safely(run())


@mcp.tool()
async def chunk_document(request: ChunkDocumentRequest) -> str:
    """Split text into overlapping chunks and optionally embed them. Returns {chunks, count, embedded, embeddingId, ...}.

    Embedding is best effort: if the gateway is unavailable the chunks are still returned with embedded=false.

    Args:
        request: text, optional chunkSize/chunkOverlap, embed (default true) and includeVectors (default false).
    """
    async def run() -> str:
        result = await get_service().chunk(request)
        return result.model_dump_json(by_alias=True)

    return await _safely(run())


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


_mcp_app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # ClusterIP only
)
# request context -> security headers -> service token (JWKS) -> tool allow-list -> MCP
_secured = secure_mcp_app(_mcp_app, settings, JWT_AUDIENCE_DOCUMENT, MCP_PATH)
app = instrument_asgi(_secured) if settings.otel_enabled else _secured

_ = DocumentErrors  # register document error codes at import


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
