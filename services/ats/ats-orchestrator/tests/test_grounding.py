"""Grounding of assistant answers: document headers are pinned, excerpts are attached to the question, file names
never reach the prompt."""
from types import SimpleNamespace

import pytest

import ats_orchestrator.api.v1  # noqa: F401  - app import order (runner <-> api.v1.chats import each other)
import ats_orchestrator.assistant.runner as runner
from agent_common.db.repositories import TenantScope
from langchain_core.messages import AIMessage, HumanMessage

from ats_orchestrator.assistant.prompts import (ACTIVE, ASSISTANT_V7, EXCERPTS_HEADING, chat_context, grounded_question,
                                          work_history)
from ats_orchestrator.constants import TurnAction
from ats_orchestrator.repositories import ChunkHit
from ats_orchestrator.services import documents

HEADER = ChunkHit(6, "resume", 0, "AMOL GAIKWAD | Engineer | London, UK | +44 7459 515493", 0.0)
SKILLS = ChunkHit(6, "resume", 3, "Built data pipelines with Kafka", 0.03)
JOBS = ChunkHit(6, "resume", 8, "Software Engineer 2012 - 2015", 0.02)
SCOPE = TenantScope(client_id=1, project_id=2)


def test_merge_puts_pinned_headers_first_without_duplicates():
    merged = documents.merge_hits([HEADER], [SKILLS, HEADER, JOBS])
    assert [h.chunk_index for h in merged] == [0, 3, 8]


def test_format_hits_labels_chunks_and_respects_the_budget():
    text = documents.format_hits([HEADER, SKILLS, JOBS], max_chars=len(HEADER.content) + 60)
    assert text.startswith("[resume #0] AMOL GAIKWAD")
    assert "Kafka" in text and "2012" not in text            # third excerpt would exceed the budget


@pytest.mark.parametrize("source, expected", [("both", [6, 9]), ("resume", [6]), ("jd", [9])])
def test_chat_document_ids_by_source(source, expected):
    chat = SimpleNamespace(resume_document_id=6, jd_document_id=9)
    assert documents.chat_document_ids(chat, source) == expected


def test_chat_document_ids_without_documents():
    assert documents.chat_document_ids(SimpleNamespace(resume_document_id=None, jd_document_id=None)) == []


def test_chat_context_never_contains_the_file_name():
    context = chat_context("Senior engineer JD", "Amol_FDE.pdf", None)
    assert "Amol_FDE" not in context and "Resume: uploaded" in context
    assert "Resume: not uploaded" in chat_context(None, None, None)


def test_chat_context_lists_the_work_history_of_the_latest_screening():
    latest = {"score": 55, "band": "Weak", "recommendation": "Do not proceed",
              "roles": [{"title": "Manager", "organization": "Publicis Sapient", "start": "2022-09", "end": "present"},
                        {"title": "Lead Engineer", "organization": "Cognizant", "start": "2021-04", "end": "2022-09"}]}
    context = chat_context("JD", "cv.pdf", latest)
    assert "  - Manager at Publicis Sapient (2022-09 - present)\n  - Lead Engineer at Cognizant" in context
    assert "Work history" not in chat_context("JD", "cv.pdf", {**latest, "roles": []})


def test_work_history_is_bounded_and_tolerates_missing_fields():
    lines = work_history([{"title": "Engineer"}] * 20, max_roles=3).splitlines()
    assert lines == ["  - Engineer at n/a (? - ?)"] * 3


def test_grounded_question_appends_excerpts_after_the_question():
    prompt = grounded_question("where does he live?", "[resume #0] London, UK")
    head, fenced = prompt.split(f"\n\n{EXCERPTS_HEADING}:\n")
    lines = fenced.splitlines()
    assert head == "where does he live?" and lines[1] == "[resume #0] London, UK"
    assert lines[0].startswith("<untrusted-documents-") and lines[2] == "</" + lines[0][1:]   # nonce fence
    assert grounded_question("hi", "") == "hi"


def test_with_excerpts_changes_only_the_latest_user_message():
    history = [HumanMessage("name?"), AIMessage("Amol Fadde"), HumanMessage("location?")]
    grounded = runner.with_excerpts(history, "[resume #0] London, UK")
    assert grounded[:2] == history[:2] and "[resume #0] London, UK\n</untrusted-documents-" in grounded[2].content
    assert history[2].content == "location?"                               # input list untouched
    assert runner.with_excerpts(history, "") is history
    assert runner.with_excerpts([AIMessage("x")], "excerpt")[0].content == "x"   # last is not a question


def test_active_prompt_requires_answers_from_excerpts():
    assert ACTIVE is ASSISTANT_V7                  # V7 = V6 + security rules
    assert "latest user message ends with DOCUMENT EXCERPTS" in ACTIVE
    assert "Never take facts from a file name" in ACTIVE


class FakeTracker:
    def __init__(self):
        self.steps = []

    def emit(self, step, message, progress=None, **extra):
        self.steps.append(step)


def _turn(content):
    return runner.TurnInput(user=None, scope=SCOPE, chat_id="c1", content=content, action=TurnAction.CHAT)


async def test_ground_searches_the_question_with_pinned_headers(monkeypatch):
    calls = []

    async def fake_search(scope, ids, query, limit, pin_headers=True):
        calls.append((scope, ids, query, limit, pin_headers))
        return [HEADER, SKILLS]

    use_container(monkeypatch, graph_facts="")
    monkeypatch.setattr(documents, "search", fake_search)
    ctx = SimpleNamespace(tracker=FakeTracker(), record={})
    chat = SimpleNamespace(resume_document_id=6, jd_document_id=9)

    excerpts = await runner._ground(_turn("what is the candidate name?"), chat, ctx)

    assert calls == [(SCOPE, [6, 9], "what is the candidate name?", 3, True)]
    assert excerpts.startswith("[resume #0] AMOL GAIKWAD") and ctx.tracker.steps == ["grounding"]
    assert runner.GRAPH_EXCERPT_LABEL not in excerpts          # graph had nothing: no empty section


class FakeKnowledge:
    def __init__(self, graph_facts):
        self.graph_facts, self.calls = graph_facts, []

    async def candidate_facts(self, scope, document_id, question):
        self.calls.append((scope, document_id, question))
        return self.graph_facts


def use_container(monkeypatch, graph_facts):
    knowledge = FakeKnowledge(graph_facts)
    settings = SimpleNamespace(grounding_chunks=3, grounding_max_chars=5000)
    monkeypatch.setattr(runner, "get_container", lambda: SimpleNamespace(settings=settings, knowledge=knowledge))
    return knowledge


async def test_ground_adds_this_candidates_graph_facts_after_the_excerpts(monkeypatch):
    async def fake_search(*args, **kwargs):
        return [HEADER]

    knowledge = use_container(monkeypatch, "- Amol Gaikwad (Candidate): location_text: London, UK")
    monkeypatch.setattr(documents, "search", fake_search)
    chat = SimpleNamespace(resume_document_id=6, jd_document_id=None)

    excerpts = await runner._ground(_turn("where is he based?"), chat, SimpleNamespace(tracker=FakeTracker(), record={}))

    assert knowledge.calls == [(SCOPE, 6, "where is he based?")]           # scoped to this chat's resume
    assert excerpts == (f"[resume #0] {HEADER.content}\n\n{runner.GRAPH_EXCERPT_LABEL}\n"
                        "- Amol Gaikwad (Candidate): location_text: London, UK")


async def test_ground_without_a_resume_does_not_ask_the_graph(monkeypatch):
    async def fake_search(*args, **kwargs):
        return []

    knowledge = use_container(monkeypatch, "never used")
    monkeypatch.setattr(documents, "search", fake_search)
    chat = SimpleNamespace(resume_document_id=None, jd_document_id=9)
    assert await runner._ground(_turn("what does the JD ask?"), chat, SimpleNamespace(tracker=FakeTracker(), record={})) == ""
    assert knowledge.calls == []


async def test_ground_skips_search_when_the_chat_has_no_documents(monkeypatch):
    async def must_not_search(*args, **kwargs):
        raise AssertionError("search must not run")

    monkeypatch.setattr(documents, "search", must_not_search)
    chat = SimpleNamespace(resume_document_id=None, jd_document_id=None)
    assert await runner._ground(_turn("hi"), chat, SimpleNamespace(tracker=FakeTracker(), record={})) == ""
