"""ats_common.knowledge: ontology formats (model + authoring), enum attributes, search filters, entity updates."""
import pytest
from pydantic import ValidationError

from ats_common.knowledge.contracts import SearchRequest, UpdateEntityRequest
from ats_common.knowledge.ontology import parse_ontology

AUTHORING = {
    "name": "cv_graph", "version": "2.0",
    "extraction_instructions": ["Create exactly ONE Candidate."],
    "entities": {
        "Candidate": {"description": "The person", "attributes": {
            "sector": {"type": "enum", "values": ["tech", "other"], "description": "Main sector"},
            "location_text": {"type": "string", "description": "Town/city and country"}}},
        "Role": {"description": "A position held"},
        "Project": {"description": "A bounded piece of work"},
        "Skill": {"description": "An ability"},
    },
    "edges": {
        "HAS_ROLE": {"description": "Candidate held this Role."},
        "PART_OF": {"description": "Part of a Role or a larger Project."},
        "APPLIES": {"description": "Applied this Skill."},
    },
    "edge_map": [
        {"source": "Candidate", "target": "Role", "edges": ["HAS_ROLE"]},
        {"source": "Project", "target": "Role", "edges": ["PART_OF"]},
        {"source": "Project", "target": "Project", "edges": ["PART_OF"]},
        {"source": "Role", "target": "Skill", "edges": ["APPLIES"]},
        {"source": "Project", "target": "Skill", "edges": ["APPLIES"]},
    ],
}


def test_authoring_format_becomes_a_validated_ontology():
    ontology = parse_ontology(AUTHORING)
    relations = {r.name: (r.source, r.target) for r in ontology.relations}
    assert ontology.version == "2.0" and ontology.extraction_instructions == ["Create exactly ONE Candidate."]
    assert ontology.entity_names == ["Candidate", "Role", "Project", "Skill"]
    assert relations["PART_OF"] == (["Project"], ["Project", "Role"])
    assert relations["APPLIES"] == (["Project", "Role"], ["Skill"])
    sector = ontology.entities[0].attributes[0]
    assert sector.type == "enum" and sector.values == ["tech", "other"]


def test_model_format_round_trips_through_its_own_json():
    ontology = parse_ontology(AUTHORING)
    assert parse_ontology(ontology.model_dump(by_alias=True)) == ontology
    assert parse_ontology(ontology.model_dump_json(by_alias=True)).cache_id == ontology.cache_id


@pytest.mark.parametrize("change, message", [
    ({"edge_map": [{"source": "Candidate", "target": "Role", "edges": ["UNKNOWN"]}]}, "undefined edge"),
    ({"edge_map": [{"source": "Candidate", "target": "Role", "edges": ["APPLIES"]},
                   {"source": "Role", "target": "Skill", "edges": ["APPLIES"]}]}, "every source x target"),
])
def test_inconsistent_edge_maps_are_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        parse_ontology({**AUTHORING, **change})


@pytest.mark.parametrize("attribute", [
    {"type": "enum", "description": "No values"},
    {"type": "string", "values": ["a"], "description": "Values on a string"},
    {"type": "enum", "values": ["a", "a"], "description": "Duplicate values"},
])
def test_invalid_enum_attributes_are_rejected(attribute):
    entities = {"Candidate": {"description": "The person", "attributes": {"sector": attribute}}}
    with pytest.raises(ValidationError):
        parse_ontology({**AUTHORING, "entities": entities, "edges": {}, "edge_map": []})


def test_empty_ontology_means_free_extraction():
    assert parse_ontology(None) is None and parse_ontology({}) is None


def test_search_filters_are_validated_and_form_a_stable_cache_key():
    request = SearchRequest(client_id=1, project_id=2, namespace="talent", question="where?",
                            subject_refs=["document:6", "document:2"], entity_types=["Candidate"])
    same = SearchRequest(client_id=1, project_id=2, namespace="talent", question="where?",
                         subject_refs=["document:2", "document:6"], entity_types=["Candidate"])
    assert request.filter_key() == same.filter_key() != SearchRequest(
        client_id=1, project_id=2, namespace="talent", question="where?").filter_key()
    with pytest.raises(ValidationError):
        SearchRequest(client_id=1, project_id=2, namespace="talent", question="where?", entity_types=["candidate"])


def test_entity_update_needs_a_change_and_safe_attribute_names():
    ok = UpdateEntityRequest(client_id=1, project_id=2, namespace="talent", entity_id="a1b2c3d4-e5f6",
                             attributes={"location_text": "London, UK", "is_current": True})
    assert ok.attributes["is_current"] is True
    for bad in ({}, {"attributes": {"uuid": "x"}}, {"attributes": {"Bad-Key": "x"}},
                {"attributes": {"notes": "x" * 1001}}):
        with pytest.raises(ValidationError):
            UpdateEntityRequest(client_id=1, project_id=2, namespace="talent", entity_id="a1b2c3d4-e5f6", **bad)
