"""Section splitting for extraction and the instructions every section gets."""
from ats_common.knowledge.ontology import parse_ontology
from ats_knowledge.shared.ontology_types import SECURITY_RULE, extraction_instructions, graphiti_types
from ats_knowledge.shared.sections import split_sections, with_context

RESUME = ("JANE DOE\nManchester, UK | jane@example.com\n\nPROFESSIONAL EXPERIENCE\n"
          "Lead Engineer, Northwind, 2022 - present\n- Led streaming migration\n\n"
          "Education:\nBSc Computer Science, Leeds, 2016")


def test_sections_follow_headings_and_keep_every_line():
    sections = split_sections(RESUME, max_chars=90)
    assert [s.splitlines()[0] for s in sections] == ["JANE DOE", "PROFESSIONAL EXPERIENCE", "Education:"]
    assert "".join(sections).replace("\n", "") == RESUME.replace("\n", "")


def test_small_sections_are_packed_up_to_the_limit():
    assert split_sections(RESUME, max_chars=4000) == [RESUME.replace("\n\n\n", "\n\n")]


def test_a_section_longer_than_the_limit_is_cut_at_line_breaks():
    long_section = "EXPERIENCE\n" + "\n".join(f"- achievement number {i}" for i in range(40))
    pieces = split_sections(long_section, max_chars=200)
    assert len(pieces) > 1 and all(len(p) <= 200 for p in pieces)
    assert all(line.startswith(("EXPERIENCE", "- achievement")) for p in pieces for line in p.splitlines())
    assert sum(p.count("achievement number") for p in pieces) == 40          # nothing dropped


def test_later_sections_say_whose_document_they_belong_to():
    sections = with_context(split_sections(RESUME, max_chars=90), "resume document:6")
    assert sections[0].startswith("JANE DOE")
    assert sections[1].startswith('(Part of resume document:6. The document begins: "JANE DOE Manchester, UK')
    assert with_context(["only one"], "x") == ["only one"]


def test_instructions_put_the_security_rule_first_then_ontology_rules_then_guidance():
    ontology = parse_ontology({"name": "cv", "version": "v1", "extraction_instructions": ["Use YYYY-MM dates."],
                               "entities": [{"name": "Candidate", "description": "The person"}]})
    text = extraction_instructions(ontology, "Every section is the same candidate.")
    assert text == f"- {SECURITY_RULE}\n- Use YYYY-MM dates.\n\nEvery section is the same candidate."
    assert extraction_instructions(None, "") == f"- {SECURITY_RULE}"


def test_enum_attributes_only_allow_their_values():
    ontology = parse_ontology({"name": "cv", "version": "v1", "entities": [{
        "name": "Candidate", "description": "The person",
        "attributes": [{"name": "sector", "type": "enum", "values": ["tech", "other"], "description": "Sector"}]}]})
    model = graphiti_types(ontology).entity_types["Candidate"]
    assert model(sector="tech").sector == "tech" and model().sector is None
    try:
        model(sector="farming")
    except ValueError:
        return
    raise AssertionError("an enum attribute accepted a value outside its list")
