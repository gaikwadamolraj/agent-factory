"""Framework-free business logic for registry-service's tools (no MCP/FastAPI here, only DB + shared services).

Each function takes a `ToolContext` (tenant-scoped session + actor + services) and a validated request model, and
returns a JSON-ready dict. The MCP tool wrappers and the REST dispatcher both call these through the shared pipeline,
so the logic is identical on both front doors and can be reused or tested without a framework (CLAUDE.md section 2).
"""
