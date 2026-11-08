from __future__ import annotations
import argparse
import os
from typing import List, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from demo_db import ensure_demo_database
from models import CertifiedTerm
from ontology_engine import DynamicAutoOntologyEngine

load_dotenv()

mcp = FastMCP(
    name="nvidia-auto-ontology-mcp",
    instructions=(
        "Semantic Ontology MCP Server. Always call `search_ontology` before writing SQL "
        "so queries use certified business definitions, formulas, and join paths."
    ),
)

engine = DynamicAutoOntologyEngine(store_path=os.getenv("ONTOLOGY_STORE_PATH", "./ontology_catalog.json"))


@mcp.tool()
def list_ontology_domains() -> List[str]:
    """Lists all compiled semantic ontology domains currently registered."""
    return list(engine.catalogs.keys())


@mcp.tool()
def compile_ontology_from_db(
    domain: str,
    db_url: Optional[str] = None,
    business_hints: Optional[str] = None,
) -> dict:
    """
    Dynamically introspects a relational database, profiles columns & foreign keys,
    and compiles a certified semantic ontology for agent reasoning.
    """
    target_db = db_url or os.getenv("DEFAULT_DB_URL") or ensure_demo_database()
    catalog = engine.compile_ontology(domain=domain, db_url=target_db, business_hints=business_hints)
    return {
        "status": "compiled",
        "domain": catalog.domain,
        "tables": list(catalog.table_summaries.keys()),
        "term_count": len(catalog.terms),
        "relationship_count": len(catalog.relationships),
    }


@mcp.tool()
def register_certified_term(
    domain: str,
    term_id: str,
    business_name: str,
    definition: str,
    source_table: str,
    mapped_columns: List[str],
    sql_formula: Optional[str] = None,
    synonyms: Optional[List[str]] = None,
) -> dict:
    """
    Dynamically registers or updates a certified business metric or entity definition
    inside the ontology (e.g. defining Net Revenue vs Gross Revenue).
    """
    term = CertifiedTerm(
        term_id=term_id,
        business_name=business_name,
        definition=definition,
        source_table=source_table,
        mapped_columns=mapped_columns,
        sql_formula=sql_formula,
        synonyms=synonyms or [],
        certified=True,
    )
    saved = engine.upsert_term(domain=domain, term=term)
    return {"status": "certified", "term": saved.model_dump()}


@mcp.tool()
def search_ontology(domain: str, query: str) -> dict:
    """
    Searches the semantic ontology for certified terms, SQL formulas, table summaries,
    and valid join relationships matching a natural-language question.
    """
    return engine.search_ontology(domain=domain, query=query)


@mcp.tool()
def validate_and_execute_sql(domain: str, sql: str, db_url: Optional[str] = None) -> dict:
    """
    Validates a read-only SQL query against the certified ontology rules, executes it
    against the target database, and returns rows alongside full term provenance.
    """
    target_db = db_url or os.getenv("DEFAULT_DB_URL") or ensure_demo_database()
    return engine.validate_and_execute_sql(domain=domain, db_url=target_db, sql=sql)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run NVIDIA Auto-Ontology MCP Server")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Bootstrap demo database if none exists
    ensure_demo_database()

    if args.transport == "streamable-http":
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
