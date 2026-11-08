"""Catalogue tool: list the registered MCP servers (invocation is the /api/v1/mcp/{server}/{tool} proxy endpoint)."""
from registry_service.authorization import ToolContext


async def list_catalogue(ctx: ToolContext, req) -> dict:
    """List the registered MCP servers the admin UI may browse/invoke (within policy), with the catalogue version."""
    catalogue = ctx.container.catalogue
    return {"servers": catalogue.list_servers(), "version": catalogue.version}
