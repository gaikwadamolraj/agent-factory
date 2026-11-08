# Dynamic NVIDIA Auto-Ontology MCP Server & Strands Agent

A complete Python implementation wrapping **NVIDIA Auto-Ontology** (`NVIDIA/auto-ontology`) inside a **FastMCP** server with **dynamic ontology generation** and a **stateless Strands Agent** consumer.

## Architecture
1. **Dynamic Auto-Ontology Engine (`ontology_engine.py`)**:
   - Connects dynamically to any SQLAlchemy-supported database (`sqlite`, `postgresql`, `duckdb`, `snowflake`, etc.).
   - Inspects live table schemas, column types, foreign keys, and sample values.
   - Uses an OpenAI-compatible / NVIDIA NIM LLM (`REASONING` role) to automatically infer business terms, certified SQL attributes/formulas, and semantic join relationships—mirroring NVIDIA Auto-Ontology's compilation pipeline.
   - Persists and updates the semantic catalog JSON dynamically at runtime.
2. **Dynamic MCP Server (`mcp_server.py`)**:
   - Exposes tools over **Model Context Protocol (MCP)** (`stdio` or `streamable-http`):
     - `compile_ontology_from_db`: Dynamically scans any database connection string and builds/updates the semantic ontology on the fly.
     - `register_certified_term`: Dynamically adds or reconciles custom business definitions & SQL metrics.
     - `search_ontology`: Resolves natural-language concepts against certified terms, columns, and join paths.
     - `validate_and_execute_sql`: Validates agent-generated SQL against the ontology rules before executing against the target DB.
3. **Stateless Strands Agent (`agent_client.py`)**:
   - Connects to the MCP server using `MCPClient` and runs stateless queries grounded strictly in certified business definitions.

## Quickstart
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 1. Start the MCP Server (Streamable HTTP on port 8000)
python mcp_server.py --transport streamable-http --port 8000

# 2. Run the Dynamic Strands Agent Client (in another terminal)
python agent_client.py
```
