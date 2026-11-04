"""Graph store port. The service talks only to this interface; Graphiti + FalkorDB is today's adapter
(`graphiti_store.py`). Another engine (Neo4j, Neptune) is a new adapter, not a change to callers."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

from ats_common.knowledge.contracts import AttributeValue, EntityHit, Fact

from ats_knowledge.shared.ontology_types import GraphitiTypes


@dataclass(frozen=True)
class EpisodeInput:
    graph_id: str
    name: str                   # short label, e.g. "resume document:42 #2"
    text: str                   # one section of the document
    source_description: str     # e.g. "resume"
    reference_time: datetime
    instructions: str           # security rule + ontology rules + caller guidance (ontology_types)
    types: GraphitiTypes | None  # None = free extraction; with types, untyped "Entity" nodes are not created


@dataclass(frozen=True)
class GraphQuery:
    graph_id: str
    text: str
    limit: int
    entity_labels: list[str] = field(default_factory=list)      # empty = all
    relation_types: list[str] = field(default_factory=list)     # empty = all
    episode_uuids: Optional[frozenset[str]] = None              # only facts extracted from these episodes
    include_expired: bool = False


@dataclass(frozen=True)
class GraphHits:
    facts: list[Fact]
    entities: list[EntityHit]


class GraphStore(Protocol):
    async def add_episode(self, episode: EpisodeInput) -> str:
        """Extract the text into the graph; returns the graph's episode id."""

    async def search(self, query: GraphQuery) -> GraphHits:
        """Hybrid (semantic + keyword + graph) search inside one tenant graph, with the query's filters."""

    async def update_entity(self, graph_id: str, entity_id: str, name: Optional[str],
                            attributes: dict[str, AttributeValue]) -> Optional[EntityHit]:
        """Rename / set attributes of one entity; None when it does not exist in this graph."""

    async def remove_episode(self, graph_id: str, graph_episode_uuid: str) -> None:
        """Delete an episode and every fact only it supported."""

    async def close(self) -> None:
        """Release connections."""
