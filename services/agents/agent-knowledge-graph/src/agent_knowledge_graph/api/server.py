"""MCP server of the shared knowledge service (streamable HTTP at /mcp). Entry point: `uvicorn agent_knowledge_graph.api.server:app`.

Any internal service can use it: add the caller to ALLOWED_CALLERS and give it tools in TOOL_POLICY.
Request pipeline (outermost first): OTEL span -> request context -> security headers -> service token (zero trust)
-> tool allow-list -> MCP. Arguments and results are camelCase (see agent_common.knowledge.contracts).
"""
import asyncio
import atexit

from agent_common.constants import JWT_AUDIENCE_KNOWLEDGE_GRAPH
from agent_common.errors import AppError, Errors
from agent_common.knowledge.contracts import (NAMESPACE_PATTERN, ForgetRequest, IngestRequest, ListEpisodesRequest,
                                            SearchRequest, TenantRef, UpdateEntityRequest)
from agent_common.knowledge.errors import KnowledgeErrors
from agent_common.logging import get_logger
from agent_common.middleware import secure_mcp_app
from agent_common.telemetry import instrument_asgi, setup_observability
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_knowledge_graph.api.container import get_service
from agent_knowledge_graph.config import get_settings

settings = get_settings()
observability = setup_observability(settings)
atexit.register(observability.shutdown)
log = get_logger("agent_knowledge_graph")
MCP_PATH = "/mcp"

mcp = MCPServer(
    name="agent-knowledge-graph",
    instructions="Shared knowledge graph. ingest_episode queues raw text for extraction into the tenant graph; "
                 "get_episode reports its status; search_knowledge answers questions from the graph (grounded, "
                 "semantic-cached); forget_source erases a source; cancel_request stops a running search.",
    debug=not settings.is_prod,
)


async def _safely(coro):
    """Tool errors carry only a catalog code + message + hint; never internal details or stack traces."""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except AppError as e:
        raise RuntimeError(f"{e.spec.code}: {e.spec.message} {e.spec.hint}") from None
    except ValueError:   # pydantic validation of tool arguments
        spec = KnowledgeErrors.INPUT_INVALID
        raise RuntimeError(f"{spec.code}: {spec.message} {spec.hint}") from None
    except Exception:
        log.exception("knowledge.tool_unhandled_error")
        raise RuntimeError(f"{Errors.INTERNAL.code}: {Errors.INTERNAL.message} {Errors.INTERNAL.hint}") from None


@mcp.tool()
async def ingest_episode(request: IngestRequest) -> str:
    """Queue raw text (a resume, a profile summary, a message) for extraction into the tenant's knowledge graph.
    Returns {episodeId, status, duplicate} at once; extraction runs in the background (poll get_episode).

    Args:
        request: clientId, projectId, namespace (use case, e.g. "talent"), sourceType, sourceRef (your id for the
            source, used by forget_source), text, guidance (what to extract), ontology (optional entity/relation
            types and extraction rules; omit for free extraction), referenceTime (when the content was true;
            default now), subjectRef (who the text is about, e.g. "document:42"; enables subject-scoped search).
            Corrections in plain language are ingested the same way (sourceType "correction").
    """
    return await _safely(_dump(get_service().ingest(request)))


@mcp.tool()
async def get_episode(clientId: int, projectId: int, namespace: str = Field(pattern=NAMESPACE_PATTERN),
                      episodeId: str = Field(pattern=r"^[0-9a-f]{32}$")) -> str:
    """Status of an ingested episode: queued, processing, done or failed (with errorCode).

    Args:
        clientId: Tenant (client) id.
        projectId: Project id.
        namespace: Use case the episode was ingested into.
        episodeId: The id returned by ingest_episode.
    """
    async def run():
        ref = TenantRef(client_id=clientId, project_id=projectId, namespace=namespace)
        return await get_service().get_episode(ref, episodeId)
    return await _safely(_dump(run()))


@mcp.tool()
async def search_knowledge(request: SearchRequest) -> str:
    """Answer a question from the tenant's knowledge graph. Returns {answer, citedFactIds, facts, entities, cached}.
    The answer only uses graph facts and cites them; answer=false returns facts only (no LLM call).

    Args:
        request: clientId, projectId, namespace, question, guidance (how to answer), ontology (optional; narrows
            to its entity types), limit (1-50), answer (default true), requestId (optional, for cancel_request),
            subjectRefs (only facts about these subjects), entityTypes / relationTypes (label filters),
            includeExpired (also facts a newer episode contradicted).
    """
    return await _safely(_dump(get_service().search(request)))


@mcp.tool()
async def forget_source(request: ForgetRequest) -> str:
    """Erase everything learned from one source (e.g. a deleted resume). Returns {forgotten: n}.

    Args:
        request: clientId, projectId, namespace, sourceRef (as given to ingest_episode).
    """
    return await _safely(_dump(get_service().forget(request)))


@mcp.tool()
async def update_entity(request: UpdateEntityRequest) -> str:
    """Fix one entity deterministically (no LLM): rename it and/or set attributes. Returns the entity.

    Args:
        request: clientId, projectId, namespace, entityId (from search_knowledge), name (optional new name),
            attributes (optional snake_case -> string/number/boolean/null).
    """
    return await _safely(_dump(get_service().update_entity(request)))


@mcp.tool()
async def list_episodes(request: ListEpisodesRequest) -> str:
    """List ingested episodes of the tenant graph, newest first. Returns {episodes: [...]} with status and sections.

    Args:
        request: clientId, projectId, namespace, subjectRef / sourceRef (optional filters), limit (1-200).
    """
    return await _safely(_dump(get_service().list_episodes(request)))


@mcp.tool()
async def cancel_request(requestId: str) -> str:
    """Cancel a running search_knowledge call (stops its LLM call). Returns {"cancelled": bool}.

    Args:
        requestId: The requestId passed to search_knowledge.
    """
    return '{"cancelled": true}' if get_service().cancel(requestId) else '{"cancelled": false}'


async def _dump(coro) -> str:
    return (await coro).model_dump_json(by_alias=True)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


_mcp_app = mcp.streamable_http_app(
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),  # ClusterIP only
)
# request context -> security headers -> service token (JWKS) -> tool allow-list -> MCP
_secured = secure_mcp_app(_mcp_app, settings, JWT_AUDIENCE_KNOWLEDGE_GRAPH, MCP_PATH)
app = instrument_asgi(_secured) if settings.otel_enabled else _secured


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
