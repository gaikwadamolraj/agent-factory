"""Knowledge (KNW) error codes. Shared so the service raises them and every caller recognises them."""
from ats_common.errors import register


class KnowledgeErrors:
    INPUT_INVALID = register("KNW-INV-0001", 422, "The knowledge request is missing data or is too large.",
                             "Check the tenant ids, namespace, text and guidance against the documented limits.")
    ONTOLOGY_INVALID = register("KNW-INV-0002", 422, "The ontology is not valid.",
                                "Fix the ontology (names, attribute types, relation source/target) and retry; "
                                "see docs/knowledge_ontology_guide.md.")
    EPISODE_NOT_FOUND = register("KNW-NFD-0001", 404, "No knowledge episode with that id exists for this tenant.",
                                 "Use the episodeId returned by ingest_episode.")
    ENTITY_NOT_FOUND = register("KNW-NFD-0002", 404, "No entity with that id exists in this tenant's graph.",
                                "Use an entityId returned by search_knowledge.")
    TEXT_TOO_LARGE = register("KNW-LIM-0001", 413, "The text is larger than the knowledge service accepts.",
                              "Split the text into smaller episodes or summarise it first.")
    GRAPH_UNAVAILABLE = register("KNW-UNA-0001", 503, "The knowledge graph is temporarily unavailable.",
                                 "Retry after retryAfter seconds.", retryable=True)
    GRAPH_TIMEOUT = register("KNW-TMO-0001", 504, "The knowledge graph did not answer in time.",
                             "Retry with a shorter question or a smaller limit.", retryable=True)
    SERVICE_UNAVAILABLE = register("KNW-UNA-0002", 503, "The knowledge service is not reachable.",
                                   "Retry after retryAfter seconds; screening and chat keep working without it.",
                                   retryable=True)
    EXTRACTION_FAILED = register("KNW-INT-0001", 500, "The text could not be turned into graph knowledge.",
                                 "Re-ingest later; if it keeps failing, use a larger model for extraction.",
                                 retryable=True)
