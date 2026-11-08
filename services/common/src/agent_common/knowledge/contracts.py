"""Wire contract of the knowledge service (MCP tool arguments and results, camelCase on the wire).

Both the service and every caller import these models, so the contract has exactly one definition.

Terms (see docs/knowledge_graph_basics.md):
* namespace  - the use case that owns a graph, e.g. "talent". Each (namespace, client, project) is its own graph.
* episode    - one piece of raw text added to the graph (a resume, a GitHub summary, a message).
* source_ref - the caller's id for where the text came from (e.g. "document:42"); used to forget it later.
* guidance   - caller instructions for the LLM: what to extract (ingest) or how to answer (search).
"""
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Optional, Union

from pydantic import Field, model_validator

from agent_common.knowledge.ontology import ATTRIBUTE_NAME as ATTRIBUTE_KEY
from agent_common.knowledge.ontology import RESERVED_ATTRIBUTES
from agent_common.models import ApiModel, ApiResponse

NAMESPACE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
SOURCE_TYPE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"
SOURCE_REF_PATTERN = r"^[A-Za-z0-9:_.@-]{1,128}$"
MAX_GUIDANCE_CHARS = 4_000


class EpisodeStatus(StrEnum):
    QUEUED = "queued"          # stored, waiting for a worker
    PROCESSING = "processing"  # a worker is extracting it into the graph
    DONE = "done"              # in the graph and searchable
    FAILED = "failed"          # gave up after the configured attempts (see errorCode)


class TenantRef(ApiModel):
    """Which tenant graph a call works on. The service builds the graph name from these validated ids only."""

    client_id: int = Field(gt=0, lt=2**31)
    project_id: int = Field(gt=0, lt=2**31)
    namespace: str = Field(pattern=NAMESPACE_PATTERN)


class IngestRequest(TenantRef):
    source_type: str = Field(pattern=SOURCE_TYPE_PATTERN, description="resume, github, linkedin, message, ...")
    source_ref: str = Field(pattern=SOURCE_REF_PATTERN)
    text: str = Field(min_length=1)
    guidance: str = Field(default="", max_length=MAX_GUIDANCE_CHARS)
    ontology: Optional[dict] = None             # Ontology as a dict; None = free extraction
    reference_time: Optional[datetime] = None   # when the content was true/written; default = now
    # Who the text is about (e.g. "document:42" for a candidate's resume). Episodes with the same subject can be
    # searched on their own (SearchRequest.subject_refs) without a separate graph per subject.
    subject_ref: Optional[str] = Field(default=None, pattern=SOURCE_REF_PATTERN)


class IngestReceipt(ApiResponse):
    episode_id: str
    status: EpisodeStatus
    duplicate: bool = False     # identical text was already ingested for this tenant + namespace


class EpisodeInfo(ApiResponse):
    episode_id: str
    source_type: str
    source_ref: str
    status: EpisodeStatus
    attempts: int
    error_code: Optional[str] = None
    updated_at: datetime
    subject_ref: Optional[str] = None
    sections: int = 0           # graph episodes (document sections) extracted so far


class SearchRequest(TenantRef):
    question: str = Field(min_length=2, max_length=2_000)
    guidance: str = Field(default="", max_length=MAX_GUIDANCE_CHARS)
    ontology: Optional[dict] = None             # narrows results to its entity types
    limit: int = Field(default=10, ge=1, le=50)
    answer: bool = True                         # False = facts only (no LLM call; callers rank them themselves)
    request_id: str = Field(default="", pattern=r"^([A-Za-z0-9_-]{6,64})?$")
    # Filters. subject_refs: only facts extracted from these subjects' episodes (e.g. one candidate);
    # entity_types / relation_types: only these labels; include_expired: also facts a newer episode contradicted.
    subject_refs: list[Annotated[str, Field(pattern=SOURCE_REF_PATTERN)]] = Field(default_factory=list,
                                                                                  max_length=50)
    entity_types: list[Annotated[str, Field(pattern=r"^[A-Z][A-Za-z0-9]{1,47}$")]] = Field(default_factory=list,
                                                                                           max_length=30)
    relation_types: list[Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,47}$")]] = Field(default_factory=list,
                                                                                           max_length=50)
    include_expired: bool = False

    def filter_key(self) -> str:
        """Stable text of the filters (part of the semantic-cache scope)."""
        return "|".join([",".join(sorted(self.subject_refs)), ",".join(sorted(self.entity_types)),
                         ",".join(sorted(self.relation_types)), str(self.include_expired)])


class Fact(ApiResponse):
    """One relation from the graph, stated as a sentence, with the time it was valid."""

    fact_id: str
    fact: str
    relation: str
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None       # set when a newer episode contradicted it


class EntityHit(ApiResponse):
    entity_id: str
    name: str
    labels: list[str] = Field(default_factory=list)
    summary: str = ""
    attributes: dict = Field(default_factory=dict)


class SearchResult(ApiResponse):
    answer: str = ""                            # empty when answer=False or nothing was found
    cited_fact_ids: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    entities: list[EntityHit] = Field(default_factory=list)
    cached: bool = False                        # served from the semantic cache
    graph_version: int = 0                      # increments whenever the tenant graph changes


class ForgetRequest(TenantRef):
    source_ref: str = Field(pattern=SOURCE_REF_PATTERN)


class ForgetResult(ApiResponse):
    forgotten: int


AttributeValue = Optional[Union[str, int, float, bool]]


class UpdateEntityRequest(TenantRef):
    """Deterministic fix of one entity (no LLM): rename it and/or set attributes. For new or corrected facts in
    plain language, ingest them as an episode instead (source_type "correction"); the graph then invalidates
    contradicted facts itself."""

    entity_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,64}$")
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict, max_length=30)

    @model_validator(mode="after")
    def _something_to_change(self) -> "UpdateEntityRequest":
        if self.name is None and not self.attributes:
            raise ValueError("give a new name or at least one attribute")
        for key, value in self.attributes.items():
            if not ATTRIBUTE_KEY.match(key) or key in RESERVED_ATTRIBUTES:
                raise ValueError(f"attribute '{key}' must be snake_case and not a reserved name")
            if isinstance(value, str) and len(value) > 1000:
                raise ValueError(f"attribute '{key}' is longer than 1000 characters")
        return self


class ListEpisodesRequest(TenantRef):
    """Episodes of one tenant graph, newest first, optionally only one subject or source."""

    subject_ref: Optional[str] = Field(default=None, pattern=SOURCE_REF_PATTERN)
    source_ref: Optional[str] = Field(default=None, pattern=SOURCE_REF_PATTERN)
    limit: int = Field(default=50, ge=1, le=200)


class EpisodeList(ApiResponse):
    episodes: list[EpisodeInfo]
