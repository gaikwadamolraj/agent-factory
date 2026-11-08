"""Search flow with in-memory fakes for the graph, cache, versions and answerer (no network, no database)."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import agent_knowledge_graph.api.service as service_module
import pytest
from agent_common.errors import AppError
from agent_common.knowledge.contracts import Fact, SearchRequest
from agent_knowledge_graph.api.answering import NOT_FOUND
from agent_knowledge_graph.api.semantic_cache import CacheLookup
from agent_knowledge_graph.api.service import KnowledgeService
from agent_knowledge_graph.shared.graph.store import GraphHits
from agent_knowledge_graph.shared.graph_guard import GraphGuard

SETTINGS = SimpleNamespace(graph_breaker_failures=5, graph_breaker_reset_s=30.0, llm_timeout_s=5.0,
                           graph_timeout_s=5.0, graph_query_timeout_s=5.0, max_answer_facts=25, max_text_chars=100)
FACT = Fact(fact_id="f1", fact="Asha has 5 years of Python", relation="HAS_SKILL")


class FakeStore:
    def __init__(self, facts):
        self.facts, self.calls = facts, []

    async def search(self, query):
        self.calls.append(query)
        return GraphHits(facts=self.facts, entities=[])


class FakeCache:
    def __init__(self):
        self.entries = {}

    async def lookup(self, scope, question):
        return CacheLookup(result=self.entries.get((scope.tag, question)), vector=[0.1])

    async def store(self, scope, question, lookup, result):
        assert lookup.vector == [0.1]          # the lookup's vector is reused, not re-embedded
        self.entries[(scope.tag, question)] = result


class FakeVersions:
    def __init__(self):
        self.version = 0

    async def current(self, graph_id):
        return self.version


class FakeAnswerer:
    def __init__(self):
        self.calls = 0

    async def answer(self, question, guidance, facts, entities, client_id=None):
        self.calls += 1
        return "Asha knows Python.", ["f1"]


def build(facts):
    store, cache, versions, answerer = FakeStore(facts), FakeCache(), FakeVersions(), FakeAnswerer()
    guard = GraphGuard("test.graph", 100, 1.0)
    return (KnowledgeService(SETTINGS, None, store, cache, versions, answerer, guard), store, cache, versions,
            answerer)


def request(**overrides):
    return SearchRequest(client_id=1, project_id=2, namespace="talent", question="Does Asha know Python?",
                         **overrides)


async def test_second_identical_question_is_served_from_cache():
    service, store, _, _, answerer = build([FACT])
    first = await service.search(request())
    second = await service.search(request())
    assert first.answer == "Asha knows Python." and not first.cached
    assert second.cached and second.answer == first.answer
    assert len(store.calls) == 1 and answerer.calls == 1
    assert store.calls[0].graph_id == "talent__c1_p2" and store.calls[0].episode_uuids is None


async def test_graph_change_invalidates_cached_answers():
    service, store, _, versions, _ = build([FACT])
    await service.search(request())
    versions.version = 1
    assert not (await service.search(request())).cached
    assert len(store.calls) == 2


async def test_empty_results_are_not_cached_and_skip_the_llm():
    service, store, cache, _, answerer = build([])
    result = await service.search(request())
    assert result.facts == [] and cache.entries == {}
    assert answerer.calls == 0 and result.answer == NOT_FOUND


async def test_facts_only_mode_makes_no_llm_call():
    service, _, _, _, answerer = build([FACT])
    result = await service.search(request(answer=False))
    assert result.facts == [FACT] and result.answer == "" and answerer.calls == 0


async def test_ontology_narrows_search_to_its_entity_types():
    service, store, _, _, _ = build([FACT])
    ontology = {"name": "talent", "version": "v1", "entities": [{"name": "Skill", "description": "A capability"}]}
    await service.search(request(ontology=ontology))
    assert store.calls[0].entity_labels == ["Skill"]


async def test_invalid_ontology_raises_catalog_error():
    service, *_ = build([FACT])
    with pytest.raises(AppError) as error:
        await service.search(request(ontology={"name": "x", "version": "1", "entities": []}))
    assert error.value.spec.code == "KNW-INV-0002"


# ---------------------------------------------------------------- filters and subject scope
class FakeDb:
    @asynccontextmanager
    async def session(self):
        yield None


def with_subject_episodes(monkeypatch, service, uuids):
    class Repository:
        def __init__(self, session):
            pass

        async def done_episode_uuids(self, graph_id, subject_refs):
            assert graph_id == "talent__c1_p2" and subject_refs == ["document:6"]
            return set(uuids)

    service.db = FakeDb()
    monkeypatch.setattr(service_module, "EpisodeRepository", Repository)


async def test_subject_scope_passes_that_subjects_episodes_to_the_graph(monkeypatch):
    service, store, *_ = build([FACT])
    with_subject_episodes(monkeypatch, service, {"ep-1", "ep-2"})
    await service.search(request(subject_refs=["document:6"], answer=False))
    assert store.calls[0].episode_uuids == frozenset({"ep-1", "ep-2"})


async def test_subject_without_extracted_episodes_returns_nothing_without_a_graph_call(monkeypatch):
    service, store, _, _, answerer = build([FACT])
    with_subject_episodes(monkeypatch, service, set())
    result = await service.search(request(subject_refs=["document:6"]))
    assert result.facts == [] and result.answer == NOT_FOUND and store.calls == [] and answerer.calls == 0


async def test_filters_reach_the_graph_and_separate_cache_entries():
    service, store, *_ = build([FACT])
    await service.search(request(entity_types=["Candidate"], relation_types=["HAS_SKILL"], include_expired=True))
    await service.search(request())
    query = store.calls[0]
    assert query.entity_labels == ["Candidate"] and query.relation_types == ["HAS_SKILL"] and query.include_expired
    assert len(store.calls) == 2          # a filtered answer is never served for an unfiltered question
