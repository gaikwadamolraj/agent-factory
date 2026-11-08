"""Ontology: an optional, caller-supplied description of the entity and relation types a knowledge graph should use.

Without an ontology the knowledge service lets the LLM decide what to extract (free extraction). With one, the
extraction is steered to these types, so every episode of a use case produces comparable nodes and relations
(what matching across many profiles needs). The service never hard-codes a domain: each caller brings its own.

    Ontology(name="talent", version="v1", entities=[
        EntityType(name="Skill", description="A capability the person has shown",
                   attributes=[Attribute(name="years", type="number", description="Years of use, if stated")]),
    ], relations=[
        RelationType(name="HAS_SKILL", description="Person has a skill", source=["Person"], target=["Skill"]),
    ], extraction_instructions=["Extract only what is written."])

Two input formats are accepted by `parse_ontology` (see docs/knowledge_ontology_guide.md):
* this model (camelCase on the wire), and
* the authoring format: {"name", "version", "extraction_instructions": [...], "entities": {Type: {description,
  attributes: {attr: {type, description, values}}}}, "edges": {EDGE: {description, attributes}},
  "edge_map": [{"source", "target", "edges": [...]}]}.
Validation is strict because names end up in graph labels and Pydantic types.
"""
import hashlib
import json
import re
from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from agent_common.models import ApiModel

ENTITY_NAME = re.compile(r"^[A-Z][A-Za-z0-9]{1,47}$")          # PascalCase: Person, Skill, Certification
RELATION_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,47}$")          # UPPER_SNAKE: HAS_SKILL, WORKED_AT
ATTRIBUTE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")         # snake_case: years, start_date
ANY_ENTITY = "Entity"                                          # wildcard for relation source/target
MAX_ENUM_VALUES = 50
MAX_INSTRUCTIONS = 40

# Field names Graphiti already uses on nodes and edges; an attribute with one of these names would overwrite them.
RESERVED_ATTRIBUTES = frozenset({
    "uuid", "name", "group_id", "labels", "created_at", "summary", "attributes", "name_embedding",
    "fact", "fact_embedding", "episodes", "expired_at", "valid_at", "invalid_at", "source_node_uuid",
    "target_node_uuid", "reference_time",
})
AttributeType = Literal["string", "number", "integer", "boolean", "date", "string_list", "enum"]


class Attribute(ApiModel):
    """One property the LLM should fill for an entity or relation (always optional at extraction time).
    `enum` attributes list their allowed `values`; the LLM can only pick one of them (or leave it empty)."""

    name: str = Field(description="snake_case property name")
    type: AttributeType = "string"
    description: str = Field(min_length=3, max_length=300, description="What the value means; guides the LLM")
    values: list[str] = Field(default_factory=list, max_length=MAX_ENUM_VALUES)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not ATTRIBUTE_NAME.match(value) or value in RESERVED_ATTRIBUTES:
            raise ValueError(f"attribute '{value}' must be snake_case and not a reserved name")
        return value

    @model_validator(mode="after")
    def _enum_values(self) -> "Attribute":
        if self.type == "enum" and not self.values:
            raise ValueError(f"enum attribute '{self.name}' needs values")
        if self.type != "enum" and self.values:
            raise ValueError(f"attribute '{self.name}' has values but is not an enum")
        if any(not 1 <= len(v) <= 64 for v in self.values) or len(set(self.values)) != len(self.values):
            raise ValueError(f"enum attribute '{self.name}': values must be unique, 1-64 characters")
        return self


class EntityType(ApiModel):
    """A kind of node, e.g. Person, Skill, Organization."""

    name: str
    description: str = Field(min_length=3, max_length=500)
    attributes: list[Attribute] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not ENTITY_NAME.match(value) or value == ANY_ENTITY:
            raise ValueError(f"entity type '{value}' must be PascalCase and not '{ANY_ENTITY}'")
        return value


class RelationType(ApiModel):
    """A kind of edge between entity types, e.g. HAS_SKILL from Person to Skill. Allowed pairs are every
    source x target combination."""

    name: str
    description: str = Field(min_length=3, max_length=500)
    source: list[str] = Field(default_factory=lambda: [ANY_ENTITY], min_length=1, max_length=10)
    target: list[str] = Field(default_factory=lambda: [ANY_ENTITY], min_length=1, max_length=10)
    attributes: list[Attribute] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not RELATION_NAME.match(value):
            raise ValueError(f"relation type '{value}' must be UPPER_SNAKE_CASE")
        return value


class Ontology(ApiModel):
    """A named, versioned set of entity and relation types plus extraction rules. Released versions are never
    edited (add a new version)."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    version: str = Field(pattern=r"^v?\d{1,4}(\.\d{1,4})?$")
    description: str = Field(default="", max_length=1000)
    extraction_instructions: list[str] = Field(default_factory=list, max_length=MAX_INSTRUCTIONS)
    entities: list[EntityType] = Field(min_length=1, max_length=30)
    relations: list[RelationType] = Field(default_factory=list, max_length=50)

    @field_validator("extraction_instructions")
    @classmethod
    def _instructions(cls, values: list[str]) -> list[str]:
        if any(not 3 <= len(v) <= 1000 for v in values):
            raise ValueError("each extraction instruction must be 3-1000 characters")
        return values

    @model_validator(mode="after")
    def _consistent(self) -> "Ontology":
        entity_names = [e.name for e in self.entities]
        relation_names = [r.name for r in self.relations]
        for kind, names in (("entity", entity_names), ("relation", relation_names)):
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate {kind} type names")
        known = set(entity_names) | {ANY_ENTITY}
        for relation in self.relations:
            unknown = set(relation.source + relation.target) - known
            if unknown:
                raise ValueError(f"relation {relation.name} refers to unknown entity types {sorted(unknown)}")
        return self

    @property
    def cache_id(self) -> str:
        """name@version:content-hash - part of cache keys, so editing an ontology never serves stale answers."""
        return f"{self.name}@{self.version}:{fingerprint(self.model_dump_json())}"

    @property
    def entity_names(self) -> list[str]:
        return [e.name for e in self.entities]


def fingerprint(*values: str) -> str:
    """Short stable hash of text values (guidance, ontology JSON)."""
    h = hashlib.sha256()
    for value in values:
        h.update(value.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def _attributes(raw: Optional[dict]) -> list[dict]:
    """Authoring-format attributes {name: {type, description, values}} -> list of Attribute dicts."""
    return [{"name": name, "type": spec.get("type", "string"), "description": spec.get("description", name),
             "values": spec.get("values", [])} for name, spec in (raw or {}).items()]


def _relations(edges: dict, edge_map: list[dict]) -> list[dict]:
    """Edges + edge_map -> RelationType dicts. An edge used for several pairs must cover every source x target
    combination of those pairs (that is how RelationType stores them); otherwise it is rejected with a clear error."""
    pairs: dict[str, set[tuple[str, str]]] = {name: set() for name in edges}
    for rule in edge_map:
        for edge in rule["edges"]:
            if edge not in pairs:
                raise ValueError(f"edge_map uses undefined edge '{edge}'")
            pairs[edge].add((rule["source"], rule["target"]))
    relations = []
    for name, spec in edges.items():
        sources = sorted({s for s, _ in pairs[name]}) or [ANY_ENTITY]
        targets = sorted({t for _, t in pairs[name]}) or [ANY_ENTITY]
        if pairs[name] and len(pairs[name]) != len(sources) * len(targets):
            raise ValueError(f"edge '{name}': its edge_map pairs must be every source x target combination")
        relations.append({"name": name, "description": (spec or {}).get("description", name),
                          "source": sources, "target": targets,
                          "attributes": _attributes((spec or {}).get("attributes"))})
    return relations


def from_authoring_format(raw: dict) -> dict:
    """The authoring format (entities/edges as dicts + edge_map) as an Ontology dict."""
    return {"name": raw["name"], "version": str(raw["version"]), "description": raw.get("description", ""),
            "extraction_instructions": list(raw.get("extraction_instructions") or []),
            "entities": [{"name": name, "description": (spec or {}).get("description", name),
                          "attributes": _attributes((spec or {}).get("attributes"))}
                         for name, spec in raw["entities"].items()],
            "relations": _relations(raw.get("edges") or {}, raw.get("edge_map") or [])}


def parse_ontology(raw: str | dict | None) -> Ontology | None:
    """Ontology from a JSON string or dict in either accepted format; None/empty means free extraction.

    Raises pydantic.ValidationError, ValueError (invalid JSON or edge_map) or KeyError (missing section) -
    callers map them to KNW-INV-0002.
    """
    if not raw:
        return None
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data.get("entities"), dict):
        data = from_authoring_format(data)
    return Ontology.model_validate(data)
