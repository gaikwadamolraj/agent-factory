from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class CertifiedTerm(BaseModel):
    term_id: str = Field(..., description="Canonical snake_case business term identifier")
    business_name: str = Field(..., description="Human-readable certified business term")
    definition: str = Field(..., description="Authoritative business meaning")
    source_table: str = Field(..., description="Primary database table holding this concept")
    mapped_columns: List[str] = Field(default_factory=list, description="Exact column names mapped to this term")
    sql_formula: Optional[str] = Field(None, description="Certified SQL expression/aggregation if metric")
    synonyms: List[str] = Field(default_factory=list, description="Alternative terms reconciled to this concept")
    certified: bool = Field(default=True, description="Whether this definition is verified for agent use")


class SemanticRelationship(BaseModel):
    name: str = Field(..., description="Semantic relationship name, e.g., customer_places_order")
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    join_type: str = Field(default="INNER JOIN")
    description: str = Field(default="")


class OntologyCatalog(BaseModel):
    domain: str
    database_url_masked: str
    terms: Dict[str, CertifiedTerm] = Field(default_factory=dict)
    relationships: List[SemanticRelationship] = Field(default_factory=list)
    table_summaries: Dict[str, str] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    is_valid: bool
    resolved_terms: List[str]
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    provenance: Dict[str, Any] = Field(default_factory=dict)
