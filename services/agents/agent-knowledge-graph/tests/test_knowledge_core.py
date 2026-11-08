"""Unit tests for the framework-free parts: ontology validation and type building, tenant graph ids, grounding,
content hashing and semantic-cache scoping."""
from datetime import UTC, date

import pytest
from agent_common.knowledge.contracts import Fact, IngestRequest, TenantRef
from agent_common.knowledge.ontology import Ontology, parse_ontology
from agent_knowledge_graph.api.answering import NOT_FOUND, _Answer, ground, render_facts
from agent_knowledge_graph.api.semantic_cache import CacheScope
from agent_knowledge_graph.api.service import content_hash
from agent_knowledge_graph.shared.ontology_types import graphiti_types
from agent_knowledge_graph.shared.tenancy import TenantGraph
from pydantic import ValidationError

ONTOLOGY = {
    "name": "talent", "version": "v1",
    "entities": [
        {"name": "Person", "description": "The candidate"},
        {"name": "Skill", "description": "A capability",
         "attributes": [{"name": "years", "type": "number", "description": "Years of use if stated"},
                        {"name": "last_used", "type": "date", "description": "When last used"}]},
    ],
    "relations": [{"name": "HAS_SKILL", "description": "Person has a skill", "source": ["Person"],
                   "target": ["Skill"]}],
}


# ---------------------------------------------------------------- ontology
def test_valid_ontology_parses_from_json_string_and_dict():
    import json
    assert parse_ontology(json.dumps(ONTOLOGY)).entities[1].attributes[0].name == "years"
    assert parse_ontology(ONTOLOGY).relations[0].name == "HAS_SKILL"
    assert parse_ontology(None) is None and parse_ontology({}) is None


@pytest.mark.parametrize("mutate", [
    lambda o: o["entities"][0].update(name="person"),                          # not PascalCase
    lambda o: o["entities"][0].update(name="Entity"),                          # reserved wildcard
    lambda o: o["entities"][1]["attributes"][0].update(name="summary"),       # reserved Graphiti field
    lambda o: o["relations"][0].update(name="has-skill"),                      # not UPPER_SNAKE
    lambda o: o["relations"][0].update(target=["Company"]),                    # unknown entity type
    lambda o: o["entities"].append({"name": "Person", "description": "dup"}),  # duplicate type
])
def test_invalid_ontology_is_rejected(mutate):
    import copy
    broken = copy.deepcopy(ONTOLOGY)
    mutate(broken)
    with pytest.raises(ValidationError):
        Ontology.model_validate(broken)


def test_ontology_cache_id_changes_with_content():
    first = Ontology.model_validate(ONTOLOGY)
    changed = Ontology.model_validate({**ONTOLOGY, "description": "edited"})
    assert first.cache_id.startswith("talent@v1:") and first.cache_id != changed.cache_id


def test_graphiti_types_build_optional_fields_and_edge_map():
    types = graphiti_types(Ontology.model_validate(ONTOLOGY))
    skill = types.entity_types["Skill"]
    assert skill.__doc__ == "A capability"
    assert skill().years is None                                   # every attribute optional
    assert skill(last_used=date(2024, 1, 1)).last_used.year == 2024
    assert types.edge_type_map == {("Person", "Skill"): ["HAS_SKILL"]}
    assert types.entity_labels == ["Person", "Skill"]
    assert graphiti_types(None) is None


# ---------------------------------------------------------------- tenancy
def test_graph_id_encodes_namespace_client_and_project():
    graph = TenantGraph.of(TenantRef(client_id=7, project_id=42, namespace="talent"))
    assert graph.graph_id == "talent__c7_p42"
    assert graph.graph_id != TenantGraph("talent", 7, 43).graph_id


@pytest.mark.parametrize("payload", [
    {"clientId": 0, "projectId": 1, "namespace": "talent"},
    {"clientId": 1, "projectId": 1, "namespace": "Talent"},
    {"clientId": 1, "projectId": 1, "namespace": "t:x"},
])
def test_tenant_ref_rejects_unsafe_values(payload):
    with pytest.raises(ValidationError):
        TenantRef.model_validate(payload)


def test_ingest_request_rejects_unsafe_source_ref():
    with pytest.raises(ValidationError):
        IngestRequest(client_id=1, project_id=1, namespace="talent", source_type="resume",
                      source_ref="doc 42; DROP", text="x")


# ---------------------------------------------------------------- grounding
FACTS = [Fact(fact_id="f1", fact="Asha has 5 years of Python", relation="HAS_SKILL"),
         Fact(fact_id="f2", fact="Asha worked at Acme", relation="WORKED_AT")]


def test_ground_keeps_only_retrieved_citations():
    answer, cited = ground(_Answer(answer="Asha knows Python.", cited_fact_ids=["f1", "invented", "f1"]), FACTS)
    assert answer == "Asha knows Python." and cited == ["f1"]


def test_ground_without_real_citation_returns_not_found():
    assert ground(_Answer(answer="Asha knows Rust.", cited_fact_ids=["x9"]), FACTS) == (NOT_FOUND, [])
    assert ground(_Answer(answer="  ", cited_fact_ids=["f1"]), FACTS) == (NOT_FOUND, [])


def test_render_facts_marks_superseded_facts():
    from datetime import datetime
    old = Fact(fact_id="f3", fact="Asha works at Beta", relation="WORKS_AT", invalid_at=datetime.now(UTC))
    text = render_facts([FACTS[0], old], [])
    assert "[f1] Asha has 5 years of Python" in text and "[f3] Asha works at Beta (no longer true)" in text


# ---------------------------------------------------------------- hashing / cache scope
def test_content_hash_depends_on_text_guidance_and_ontology():
    ontology = Ontology.model_validate(ONTOLOGY)
    base = content_hash("resume", "g", None)
    assert base == content_hash("resume", "g", None)
    assert len({base, content_hash("resume2", "g", None), content_hash("resume", "g2", None),
                content_hash("resume", "g", ontology)}) == 4


def test_cache_scope_isolates_tenants_versions_and_instructions():
    scope = CacheScope("talent__c1_p1", 3, "g", "free", True)
    assert scope.tag == CacheScope("talent__c1_p1", 3, "g", "free", True).tag
    others = [CacheScope("talent__c2_p1", 3, "g", "free", True), CacheScope("talent__c1_p1", 4, "g", "free", True),
              CacheScope("talent__c1_p1", 3, "h", "free", True), CacheScope("talent__c1_p1", 3, "g", "o", True),
              CacheScope("talent__c1_p1", 3, "g", "free", False)]
    assert all(other.tag != scope.tag for other in others)
