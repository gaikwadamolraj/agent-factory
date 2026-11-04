"""Talent knowledge glue: guidance versions, what is sent to the knowledge service, and graceful degradation."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from ats_common.db.repositories import TenantScope
from ats_common.errors import AppError
from ats_common.knowledge.contracts import EntityHit, Fact, IngestReceipt, SearchResult
from ats_common.knowledge.errors import KnowledgeErrors
from orchestrator.knowledge import GRAPH_EMPTY, TalentKnowledge, profile_text, screening_text
from orchestrator.knowledge.guidance import DEFAULT_VERSION, load_guidance, load_ontology

SETTINGS = SimpleNamespace(knowledge_guidance_version="", knowledge_namespace="talent",
                           knowledge_ingest_screenings=True, knowledge_max_text_chars=50, knowledge_search_facts=10)
SCOPE = TenantScope(client_id=3, project_id=9)
RESULT = {"score": 64, "band": "fair", "recommendation": "maybe", "roleTitle": "Analyst",
          "requirements": [{"name": "Reporting", "grade": "strong", "importance": "must"},
                           {"name": "Forecasting", "grade": "absent", "importance": "should"}]}


class FakeClient:
    def __init__(self, search_result=None, error=None):
        self.ingested, self.searches, self.search_result, self.error = [], [], search_result, error

    async def ingest(self, request):
        self.ingested.append(request)
        return IngestReceipt(episode_id="e" * 32, status="queued")

    async def search(self, request):
        self.searches.append(request)
        if self.error:
            raise AppError(self.error)
        return self.search_result

    async def cancel(self, request_id):
        return True


async def _drain():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_unknown_guidance_version_falls_back_to_default():
    assert load_guidance("v999").version == DEFAULT_VERSION
    assert load_guidance("").version == DEFAULT_VERSION


def test_screening_text_has_outcome_but_no_jd_text():
    text = screening_text(5, {**RESULT, "jdText": "SECRET JD"})
    assert "document:5" in text and "64/100" in text
    assert "Requirements met: Reporting" in text and "Forecasting (should)" in text
    assert "SECRET JD" not in text


async def test_profile_and_screening_share_the_source_ref_and_respect_limits():
    client = FakeClient()
    knowledge = TalentKnowledge(SETTINGS, client)
    knowledge.remember_profile(SCOPE, 5, "Jane Doe " * 20)
    knowledge.remember_screening(SCOPE, 5, RESULT)
    await _drain()
    profile, screening = client.ingested
    assert profile.source_ref == screening.source_ref == "document:5"
    assert (profile.client_id, profile.project_id, profile.namespace) == (3, 9, "talent")
    assert len(profile.text) <= 50 and profile.text.startswith(profile_text(5, "")[:20])   # truncated, stripped
    assert "Jane Doe" in profile.text
    assert profile.guidance == load_guidance("").profile_extraction


async def test_screening_ingest_can_be_switched_off():
    client = FakeClient()
    TalentKnowledge(SimpleNamespace(**{**vars(SETTINGS), "knowledge_ingest_screenings": False}),
                    client).remember_screening(SCOPE, 5, RESULT)
    await _drain()
    assert client.ingested == []


async def test_disabled_knowledge_is_a_no_op_with_a_clear_answer():
    knowledge = TalentKnowledge(SETTINGS, None)
    knowledge.remember_profile(SCOPE, 1, "text")
    assert not knowledge.enabled
    assert "not enabled" in await knowledge.ask(SCOPE, "who knows reporting?", "req123")


async def test_ask_returns_graph_facts_without_an_extra_llm_answer():
    from datetime import UTC, datetime
    result = SearchResult(facts=[Fact(fact_id="f2", fact="Jane lived in Pune", relation="LIVES_IN",
                                      invalid_at=datetime.now(UTC)),
                                 Fact(fact_id="f1", fact="Jane lives in London", relation="LIVES_IN")])
    client = FakeClient(search_result=result)
    answer = await TalentKnowledge(SETTINGS, client).ask(SCOPE, "where does Jane live?", "req123")
    assert answer.index("Jane lives in London") < answer.index("Jane lived in Pune (no longer true)")  # current first
    request = client.searches[0]
    assert request.answer is False and request.limit == 10 and request.request_id == "req123"


async def test_ask_on_an_empty_graph_points_to_search_documents():
    answer = await TalentKnowledge(SETTINGS, FakeClient(search_result=SearchResult())).ask(SCOPE, "where?", "req123")
    assert answer == GRAPH_EMPTY and "search_documents" in answer


def test_active_prompt_routes_this_resume_questions_to_search_documents():
    from orchestrator.assistant.prompts import ACTIVE, ASSISTANT_V3
    assert "CHOOSING A TOOL FOR CANDIDATE QUESTIONS" in ACTIVE and ACTIVE != ASSISTANT_V3
    assert ACTIVE.index("search_documents first") < ACTIVE.index("RULES\n- Never invent")


async def test_ask_turns_service_errors_into_a_friendly_message():
    client = FakeClient(error=KnowledgeErrors.SERVICE_UNAVAILABLE)
    answer = await TalentKnowledge(SETTINGS, client).ask(SCOPE, "who does reporting?", "req123")
    assert answer.startswith("The knowledge graph could not answer:")


# ---------------------------------------------------------------- v2: ontology, subject scope, grounding facts
GROUNDING = SimpleNamespace(**{**vars(SETTINGS), "knowledge_max_text_chars": 1000, "knowledge_grounding_facts": 8,
                               "knowledge_grounding_timeout_s": 0.05})


def test_default_guidance_extracts_profiles_with_the_career_ontology():
    ontology = load_ontology(load_guidance("").profile_ontology)
    assert ontology["name"] == "generic_cv_career_graph" and "Candidate" in ontology["entities"]
    assert load_ontology(load_guidance("v1").profile_ontology) is None          # v1 stays free extraction


async def test_profile_carries_ontology_and_candidate_subject_screening_only_the_subject():
    client = FakeClient()
    knowledge = TalentKnowledge(GROUNDING, client)
    knowledge.remember_profile(SCOPE, 5, "Jane Doe, London")
    knowledge.remember_screening(SCOPE, 5, RESULT)
    await _drain()
    profile, screening = client.ingested
    assert profile.subject_ref == screening.subject_ref == "document:5"
    assert profile.ontology["name"] == "generic_cv_career_graph" and screening.ontology is None


async def test_candidate_facts_are_scoped_to_the_resume_and_formatted_with_attributes():
    graph = SearchResult(
        entities=[EntityHit(entity_id="n1", name="Jane Doe", labels=["Candidate"],
                            attributes={"location_text": "London, UK", "phone": None})],
        facts=[Fact(fact_id="f1", fact="Jane Doe works at Northwind", relation="HAS_ROLE"),
               Fact(fact_id="f2", fact="Jane Doe works at Contoso", relation="HAS_ROLE",
                    invalid_at=datetime(2021, 12, 1, tzinfo=UTC))])
    client = FakeClient(search_result=graph)
    lines = await TalentKnowledge(GROUNDING, client).candidate_facts(SCOPE, 5, "where is she?")
    request = client.searches[0]
    assert request.subject_refs == ["document:5"] and request.answer is False and request.limit == 8
    assert lines == "- Jane Doe (Candidate): location_text: London, UK\n- Jane Doe works at Northwind"


async def test_candidate_facts_never_fail_or_delay_a_turn():
    class SlowClient(FakeClient):
        async def search(self, request):
            await asyncio.sleep(5)

    assert await TalentKnowledge(GROUNDING, SlowClient()).candidate_facts(SCOPE, 5, "where?") == ""
    failing = FakeClient(error=KnowledgeErrors.SERVICE_UNAVAILABLE)
    assert await TalentKnowledge(GROUNDING, failing).candidate_facts(SCOPE, 5, "where?") == ""
    assert await TalentKnowledge(GROUNDING, None).candidate_facts(SCOPE, 5, "where?") == ""
