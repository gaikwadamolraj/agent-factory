"""GraphStore adapter: Graphiti (temporal knowledge graph) on FalkorDB; LLM and embeddings through the
LLM gateway (gateway_clients.py, ats_common.llm). Each tenant graph uses its client's extraction model alias.

Isolation: one FalkorDB graph per tenant graph id. Each graph gets its own Graphiti instance bound to a driver for
that graph (`driver.clone(graph_id)` shares the connection pool), kept in a bounded LRU. A query can therefore only
ever touch the graph it was opened for.

Search uses reciprocal-rank fusion of keyword (BM25) and vector results; no cross-encoder, because a local
OpenAI-compatible server does not provide the log-probabilities Graphiti's reranker needs.
"""
import asyncio
from collections import OrderedDict
from typing import Optional

from ats_common.knowledge.contracts import AttributeValue, EntityHit, Fact
from ats_common.logging import get_logger, log_event
from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EntityNode, EpisodeType
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import SearchFilters

from ats_knowledge.config import KnowledgeSettings
from ats_knowledge.shared.graph.gateway_clients import GatewayEmbedder, extraction_llm, unused_reranker
from ats_knowledge.shared.graph.store import EpisodeInput, GraphHits, GraphQuery
from ats_knowledge.shared.tenancy import TenantGraph

log = get_logger(__name__)
MAX_SUMMARY_CHARS = 600
MAX_FETCH = 50                  # results fetched for one subject-scoped search before filtering
GENERIC_ENTITY = "Entity"       # Graphiti's label for nodes that fit no ontology type


def _entity(node: EntityNode) -> EntityHit:
    return EntityHit(entity_id=node.uuid, name=node.name, labels=[x for x in node.labels if x != GENERIC_ENTITY],
                     summary=(node.summary or "")[:MAX_SUMMARY_CHARS], attributes=node.attributes or {})


class GraphitiStore:
    def __init__(self, settings: KnowledgeSettings):
        self.settings = settings
        password = settings.falkordb_password.get_secret_value() if settings.falkordb_password else None
        self._driver = FalkorDriver(host=settings.falkordb_host, port=settings.falkordb_port,
                                    username=settings.falkordb_username, password=password)
        self._embedder = GatewayEmbedder()
        self._reranker = unused_reranker()
        self._graphs: OrderedDict[str, Graphiti] = OrderedDict()
        self._indexed: set[str] = set()
        self._open_lock = asyncio.Lock()

    async def _graph(self, graph_id: str) -> Graphiti:
        """Graphiti bound to one tenant graph (LRU-cached; indices created once per graph and process)."""
        async with self._open_lock:
            graph = self._graphs.get(graph_id)
            if graph is None:
                graph = Graphiti(graph_driver=self._driver.clone(database=graph_id),
                                 llm_client=extraction_llm(TenantGraph.client_of(graph_id)),
                                 embedder=self._embedder, cross_encoder=self._reranker,
                                 max_coroutines=self.settings.llm_concurrency)
                self._graphs[graph_id] = graph
                if len(self._graphs) > self.settings.graph_cache_size:
                    self._graphs.popitem(last=False)   # clones share the pool: dropping one closes nothing
            self._graphs.move_to_end(graph_id)
            if graph_id not in self._indexed:
                await graph.build_indices_and_constraints()
                self._indexed.add(graph_id)
                log_event(log, "knowledge.graph_opened", graph_id=graph_id)
            return graph

    async def add_episode(self, episode: EpisodeInput) -> str:
        graph = await self._graph(episode.graph_id)
        types = episode.types
        result = await graph.add_episode(
            name=episode.name, episode_body=episode.text, source_description=episode.source_description,
            reference_time=episode.reference_time, source=EpisodeType.text, group_id=episode.graph_id,
            entity_types=types.entity_types if types else None,
            excluded_entity_types=[GENERIC_ENTITY] if types else None,   # only ontology types are created
            edge_types=types.edge_types if types else None,
            edge_type_map=types.edge_type_map if types else None,
            custom_extraction_instructions=episode.instructions or None)
        log_event(log, "knowledge.episode_extracted", graph_id=episode.graph_id, nodes=len(result.nodes),
                  edges=len(result.edges))
        return result.episode.uuid

    async def search(self, query: GraphQuery) -> GraphHits:
        """Hybrid search. With episode_uuids (one subject), more results are fetched and only facts extracted
        from those episodes are kept; their end nodes are then loaded exactly (with all attributes)."""
        graph = await self._graph(query.graph_id)
        scoped = query.episode_uuids is not None
        fetch = min(query.limit * self.settings.subject_search_overfetch, MAX_FETCH) if scoped else query.limit
        config = COMBINED_HYBRID_SEARCH_RRF.model_copy(update={"limit": fetch})
        results = await graph.search_(query.text, config=config, group_ids=[query.graph_id],
                                      search_filter=SearchFilters(node_labels=query.entity_labels or None,
                                                                  edge_types=query.relation_types or None))
        edges = [e for e in results.edges
                 if (query.include_expired or (e.invalid_at is None and e.expired_at is None))
                 and (not scoped or query.episode_uuids.intersection(e.episodes or []))][: query.limit]
        if scoped:
            ends = list({uuid for e in edges for uuid in (e.source_node_uuid, e.target_node_uuid)})
            nodes = await EntityNode.get_by_uuids(graph.driver, ends) if ends else []
            labels = set(query.entity_labels)
            nodes = [n for n in nodes if not labels or labels.intersection(n.labels)]
        else:
            nodes = results.nodes
        facts = [Fact(fact_id=e.uuid, fact=e.fact, relation=e.name, valid_at=e.valid_at, invalid_at=e.invalid_at)
                 for e in edges]
        return GraphHits(facts=facts, entities=[_entity(n) for n in nodes[: query.limit]])

    async def update_entity(self, graph_id: str, entity_id: str, name: Optional[str],
                            attributes: dict[str, AttributeValue]) -> Optional[EntityHit]:
        """Deterministic fix of one node of this graph (the graph's own driver cannot reach another tenant)."""
        graph = await self._graph(graph_id)
        try:
            node = await EntityNode.get_by_uuid(graph.driver, entity_id)
        except NodeNotFoundError:
            return None
        if node.group_id != graph_id:
            return None
        node.attributes = {**(node.attributes or {}), **attributes}
        if name:
            node.name = name.strip()
            await node.generate_name_embedding(self._embedder)
        await node.save(graph.driver)
        log_event(log, "knowledge.entity_updated", graph_id=graph_id, renamed=bool(name),
                  attributes=sorted(attributes))
        return _entity(node)

    async def remove_episode(self, graph_id: str, graph_episode_uuid: str) -> None:
        graph = await self._graph(graph_id)
        await graph.remove_episode(graph_episode_uuid)

    async def close(self) -> None:
        await self._driver.close()
