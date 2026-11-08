from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Any, Optional

from openai import OpenAI
from sqlalchemy import create_engine, inspect, text

from models import (
    CertifiedTerm,
    OntologyCatalog,
    SemanticRelationship,
    ValidationResult,
)


class DynamicAutoOntologyEngine:
    """
    Dynamically inspects relational schemas, generates an LLM-inferred semantic ontology,
    reconciles business definitions, and validates agent SQL queries with provenance.
    """

    def __init__(self, store_path: str = "./ontology_catalog.json"):
        self.store_path = Path(store_path)
        self.base_url = os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.reasoning_model = os.getenv("REASONING_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
        self.catalogs: Dict[str, OntologyCatalog] = {}
        self._load_store()

    def _load_store(self) -> None:
        if self.store_path.exists():
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
            for domain, data in raw.items():
                self.catalogs[domain] = OntologyCatalog.model_validate(data)

    def _save_store(self) -> None:
        serialized = {k: v.model_dump() for k, v in self.catalogs.items()}
        self.store_path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")

    @staticmethod
    def _mask_db_url(db_url: str) -> str:
        return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", db_url)

    def profile_database(self, db_url: str, sample_rows: int = 3) -> Dict[str, Any]:
        """Extracts tables, columns, data types, foreign keys, and sample rows."""
        engine = create_engine(db_url)
        inspector = inspect(engine)
        schema_profile: Dict[str, Any] = {"tables": {}}

        with engine.connect() as conn:
            for table_name in inspector.get_table_names():
                cols = inspector.get_columns(table_name)
                fks = inspector.get_foreign_keys(table_name)
                rows = conn.execute(text(f"SELECT * FROM {table_name} LIMIT :lim"), {"lim": sample_rows}).mappings().all()
                schema_profile["tables"][table_name] = {
                    "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]} for c in cols],
                    "foreign_keys": fks,
                    "sample_rows": [dict(r) for r in rows],
                }
        return schema_profile

    def compile_ontology(self, domain: str, db_url: str, business_hints: Optional[str] = None) -> OntologyCatalog:
        """
        Dynamically builds a semantic ontology from a live database schema.
        Uses NVIDIA NIM / OpenAI-compatible reasoning model when API key is configured,
        or falls back to deterministic structural extraction.
        """
        profile = self.profile_database(db_url)

        if self.api_key and not self.api_key.startswith("nvapi-your"):
            catalog = self._llm_compile(domain, db_url, profile, business_hints)
        else:
            catalog = self._heuristic_compile(domain, db_url, profile)

        self.catalogs[domain] = catalog
        self._save_store()
        return catalog

    def _llm_compile(
        self, domain: str, db_url: str, profile: Dict[str, Any], business_hints: Optional[str]
    ) -> OntologyCatalog:
        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        prompt = f"""You are NVIDIA Auto-Ontology compiler. Given this database schema profile and sample data,
generate a certified semantic ontology JSON matching this schema:
{{
  "domain": "{domain}",
  "database_url_masked": "{self._mask_db_url(db_url)}",
  "table_summaries": {{"table_name": "purpose"}},
  "terms": {{
    "term_id": {{
      "term_id": "snake_case_id",
      "business_name": "Human Name",
      "definition": "Clear business rule (e.g. excludes CANCELLED orders)",
      "source_table": "table_name",
      "mapped_columns": ["col1"],
      "sql_formula": "Optional SQL formula e.g. SUM(gross_amt - discount_amt)",
      "synonyms": ["alias1"],
      "certified": true
    }}
  }},
  "relationships": [
    {{
      "name": "rel_name",
      "from_table": "t1",
      "from_column": "c1",
      "to_table": "t2",
      "to_column": "c2",
      "join_type": "INNER JOIN",
      "description": "How entities join"
    }}
  ]
}}

Business Hints: {business_hints or "None provided"}
Database Profile:
{json.dumps(profile, default=str)}
Return ONLY valid JSON."""

        resp = client.chat.completions.create(
            model=self.reasoning_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return OntologyCatalog.model_validate(data)

    def _heuristic_compile(self, domain: str, db_url: str, profile: Dict[str, Any]) -> OntologyCatalog:
        """Fallback structural compiler when running offline without an API key."""
        terms: Dict[str, CertifiedTerm] = {}
        relationships: List[SemanticRelationship] = []
        summaries: Dict[str, str] = {}

        for table, info in profile["tables"].items():
            col_names = [c["name"] for c in info["columns"]]
            summaries[table] = f"Auto-discovered entity table '{table}' with columns: {', '.join(col_names)}"

            for col in info["columns"]:
                cname = col["name"]
                term_id = f"{table}_{cname}".lower()
                terms[term_id] = CertifiedTerm(
                    term_id=term_id,
                    business_name=f"{table.title()} {cname.replace('_', ' ').title()}",
                    definition=f"Column '{cname}' ({col['type']}) from table '{table}'.",
                    source_table=table,
                    mapped_columns=[cname],
                    synonyms=[cname],
                    certified=True,
                )

            for fk in info.get("foreign_keys", []):
                if fk.get("constrained_columns") and fk.get("referred_columns"):
                    relationships.append(
                        SemanticRelationship(
                            name=f"{table}_to_{fk['referred_table']}",
                            from_table=table,
                            from_column=fk["constrained_columns"][0],
                            to_table=fk["referred_table"],
                            to_column=fk["referred_columns"][0],
                            description=f"Foreign key join between {table} and {fk['referred_table']}",
                        )
                    )

        return OntologyCatalog(
            domain=domain,
            database_url_masked=self._mask_db_url(db_url),
            terms=terms,
            relationships=relationships,
            table_summaries=summaries,
        )

    def upsert_term(self, domain: str, term: CertifiedTerm) -> CertifiedTerm:
        if domain not in self.catalogs:
            raise ValueError(f"Domain '{domain}' not compiled yet. Call compile_ontology_from_db first.")
        self.catalogs[domain].terms[term.term_id] = term
        self._save_store()
        return term

    def search_ontology(self, domain: str, query: str) -> Dict[str, Any]:
        if domain not in self.catalogs:
            raise ValueError(f"Unknown domain '{domain}'. Available: {list(self.catalogs.keys())}")
        catalog = self.catalogs[domain]
        q_tokens = set(re.findall(r"[a-z0-9_]+", query.lower()))

        matched_terms = []
        for term in catalog.terms.values():
            haystack = " ".join(
                [term.term_id, term.business_name, term.definition, term.source_table]
                + term.mapped_columns
                + term.synonyms
            ).lower()
            score = sum(1 for tok in q_tokens if tok in haystack)
            if score > 0:
                matched_terms.append((score, term.model_dump()))

        matched_terms.sort(key=lambda x: x[0], reverse=True)
        return {
            "domain": domain,
            "matched_terms": [m[1] for m in matched_terms[:10]],
            "relationships": [r.model_dump() for r in catalog.relationships],
            "table_summaries": catalog.table_summaries,
        }

    def validate_and_execute_sql(self, domain: str, db_url: str, sql: str) -> Dict[str, Any]:
        if domain not in self.catalogs:
            raise ValueError(f"Unknown domain '{domain}'.")
        catalog = self.catalogs[domain]
        sql_clean = sql.strip()
        sql_lower = sql_clean.lower()

        errors: List[str] = []
        warnings: List[str] = []

        # Read-only guardrail
        if not sql_lower.startswith("select") and not sql_lower.startswith("with"):
            errors.append("Security Gate: Only read-only SELECT / WITH queries are permitted.")

        # Verify referenced tables exist in certified ontology
        known_tables = set(catalog.table_summaries.keys())
        referenced_tables = [t for t in known_tables if re.search(rf"\b{re.escape(t)}\b", sql_lower)]
        if not referenced_tables:
            errors.append(f"Ontology Gate: Query must reference at least one certified table: {sorted(known_tables)}")

        # Resolve which certified terms are used in the SQL
        resolved_terms = []
        provenance = {}
        for term in catalog.terms.values():
            if term.source_table in referenced_tables and any(
                re.search(rf"\b{re.escape(col.lower())}\b", sql_lower) for col in term.mapped_columns
            ):
                resolved_terms.append(term.term_id)
                provenance[term.term_id] = {
                    "business_name": term.business_name,
                    "definition": term.definition,
                    "sql_formula": term.sql_formula,
                    "source_table": term.source_table,
                }

        if not resolved_terms:
            warnings.append("No specific certified business terms matched columns in SQL.")

        validation = ValidationResult(
            is_valid=len(errors) == 0,
            resolved_terms=resolved_terms,
            warnings=warnings,
            errors=errors,
            provenance=provenance,
        )

        if not validation.is_valid:
            return {"validation": validation.model_dump(), "rows": []}

        engine = create_engine(db_url)
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(text(sql_clean)).mappings().all()]

        return {
            "validation": validation.model_dump(),
            "row_count": len(rows),
            "rows": rows[:100],
        }
