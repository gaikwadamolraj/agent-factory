"""Shared knowledge-graph contract: ontology schema, wire models, error codes and (with the `mcp` extra) the client.

The client is not imported here so services that only need the models do not need the MCP SDK:
`from agent_common.knowledge.client import KnowledgeClient`.
"""
from agent_common.knowledge.contracts import (EntityHit, EpisodeInfo, EpisodeList, EpisodeStatus, Fact, ForgetRequest,
                                            ForgetResult, IngestReceipt, IngestRequest, ListEpisodesRequest,
                                            SearchRequest, SearchResult, TenantRef, UpdateEntityRequest)
from agent_common.knowledge.errors import KnowledgeErrors
from agent_common.knowledge.ontology import Attribute, EntityType, Ontology, RelationType, parse_ontology

__all__ = ["Attribute", "EntityHit", "EntityType", "EpisodeInfo", "EpisodeList", "EpisodeStatus", "Fact",
           "ForgetRequest", "ForgetResult", "IngestReceipt", "IngestRequest", "KnowledgeErrors",
           "ListEpisodesRequest", "Ontology", "RelationType", "SearchRequest", "SearchResult", "TenantRef",
           "UpdateEntityRequest", "parse_ontology"]
