"""Worker extraction step: one graph episode per section, progress recorded per section, retry resumes,
done / retry with backoff / final failure recorded in the ledger."""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import agent_knowledge_graph.worker.extraction as extraction
from agent_knowledge_graph.shared.episodes import KnowledgeEpisode
from agent_knowledge_graph.shared.graph_guard import GraphGuard
from agent_knowledge_graph.shared.ontology_types import SECURITY_RULE
from agent_knowledge_graph.worker.extraction import EpisodeExtractor

SETTINGS = SimpleNamespace(graph_timeout_s=5.0, worker_max_attempts=3, episode_max_chars=60)
RESUME = "JANE DOE\nLondon, UK\n\nEXPERIENCE\nLead Engineer, Northwind, 2022 - present\n\nEDUCATION\nBSc, Leeds"
ONTOLOGY = {"name": "cv", "version": "2.0", "extraction_instructions": ["Create exactly ONE Candidate."],
            "entities": {"Candidate": {"description": "The person", "attributes": {
                "location_text": {"type": "string", "description": "Town/city and country"}}}}}


class FakeDb:
    @asynccontextmanager
    async def session(self):
        yield None


class FakeRepository:
    calls: list = []

    def __init__(self, session):
        pass

    async def add_section(self, episode_id, graph_episode_uuid):
        FakeRepository.calls.append(("section", episode_id, graph_episode_uuid))

    async def mark_done(self, episode_id):
        FakeRepository.calls.append(("done", episode_id))

    async def mark_failed(self, episode_id, code, retry_in_s):
        FakeRepository.calls.append(("failed", episode_id, code, retry_in_s))

    async def release(self, episode_id):
        FakeRepository.calls.append(("released", episode_id))


class FakeStore:
    def __init__(self, fail_on_call=None):
        self.fail_on_call, self.inputs = fail_on_call, []

    async def add_episode(self, episode_input):
        self.inputs.append(episode_input)
        if self.fail_on_call == len(self.inputs):
            raise RuntimeError("LLM 500")
        return f"graph-episode-{len(self.inputs)}"


class FakeVersions:
    def __init__(self):
        self.bumped = []

    async def bump(self, graph_id):
        self.bumped.append(graph_id)


def episode(attempts: int, done_sections: list[str] | None = None, ontology=None) -> KnowledgeEpisode:
    return KnowledgeEpisode(id="e1", client_id=1, project_id=2, namespace="talent", graph_id="talent__c1_p2",
                            source_type="resume", source_ref="document:5", content_hash="h", content=RESUME,
                            guidance="extract", ontology=ontology, reference_time=datetime.now(UTC),
                            attempts=attempts, graph_episode_uuids=done_sections or [])


def build(monkeypatch, store):
    FakeRepository.calls = []
    monkeypatch.setattr(extraction, "EpisodeRepository", FakeRepository)
    versions = FakeVersions()
    return EpisodeExtractor(SETTINGS, FakeDb(), store, versions, GraphGuard("test.extract", 100, 1.0)), versions


async def test_each_section_is_extracted_and_recorded_then_done(monkeypatch):
    store = FakeStore()
    extractor, versions = build(monkeypatch, store)
    await extractor.process(episode(attempts=1))
    sections = [c for c in FakeRepository.calls if c[0] == "section"]
    assert len(store.inputs) == len(sections) == 3 and FakeRepository.calls[-1] == ("done", "e1")
    assert versions.bumped == ["talent__c1_p2"]
    assert store.inputs[0].text.startswith("JANE DOE") and store.inputs[0].name == "resume document:5 #1"
    assert "The document begins: \"JANE DOE" in store.inputs[1].text      # later sections know whose resume
    assert store.inputs[0].instructions.startswith(f"- {SECURITY_RULE}") and store.inputs[0].instructions.endswith(
        "extract")
    assert store.inputs[0].types is None                                  # no ontology -> free extraction


async def test_ontology_rules_and_types_reach_every_section(monkeypatch):
    store = FakeStore()
    extractor, _ = build(monkeypatch, store)
    await extractor.process(episode(attempts=1, ontology=ONTOLOGY))
    assert all("Create exactly ONE Candidate." in i.instructions for i in store.inputs)
    assert all(list(i.types.entity_types) == ["Candidate"] for i in store.inputs)


async def test_retry_resumes_after_the_sections_already_extracted(monkeypatch):
    store = FakeStore()
    extractor, _ = build(monkeypatch, store)
    await extractor.process(episode(attempts=2, done_sections=["graph-episode-a", "graph-episode-b"]))
    assert len(store.inputs) == 1 and store.inputs[0].name == "resume document:5 #3"


async def test_failure_keeps_finished_sections_and_backs_off(monkeypatch):
    extractor, versions = build(monkeypatch, FakeStore(fail_on_call=2))
    await extractor.process(episode(attempts=1))
    assert FakeRepository.calls == [("section", "e1", "graph-episode-1"), ("failed", "e1", "KNW-INT-0001", 60.0)]
    assert versions.bumped == []


async def test_backoff_doubles_and_last_attempt_fails_for_good(monkeypatch):
    extractor, _ = build(monkeypatch, FakeStore(fail_on_call=1))
    await extractor.process(episode(attempts=2))
    extractor.store = FakeStore(fail_on_call=1)
    await extractor.process(episode(attempts=3))
    assert [c[3] for c in FakeRepository.calls] == [120.0, None]


async def test_shutdown_mid_extraction_releases_the_episode(monkeypatch):
    class SlowStore(FakeStore):
        async def add_episode(self, episode_input):
            await asyncio.sleep(10)

    extractor, versions = build(monkeypatch, SlowStore())
    task = asyncio.create_task(extractor.process(episode(attempts=1)))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert FakeRepository.calls == [("released", "e1")] and versions.bumped == []
