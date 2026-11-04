"""ats_common.llm: model policy per client, cache identity, guarded calls (error mapping, retry), embeddings."""
import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import BaseModel

import ats_common.llm.calls as calls
import ats_common.llm.embeddings as embeddings
from ats_common.errors import AppError, Errors
from ats_common.llm import (LLMSettings, Purpose, embed_documents, embed_query, embed_text, guarded, model_for,
                            model_identity, profile_of, reset_llm_settings, structured_completion)

PROFILES = '{"high-quality": {"chat": "ats-chat-quality", "embedding": "never-used"}}'


@pytest.fixture(autouse=True)
def llm_env(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gateway/v1")
    monkeypatch.setenv("LLM_PROFILES", PROFILES)
    monkeypatch.setenv("LLM_CLIENT_PROFILES", '{"2": "high-quality", "3": "missing-profile"}')
    monkeypatch.setenv("LLM_CONFIG_VERSION", "7")
    monkeypatch.setenv("LLM_EMBEDDING_DIM", "3")
    reset_llm_settings()
    calls.gateway_client.cache_clear()
    yield
    reset_llm_settings()


# ---------------------------------------------------------------- policy
def test_settings_parse_json_policies_from_env():
    settings = LLMSettings()
    assert settings.profiles["high-quality"]["chat"] == "ats-chat-quality"
    assert settings.client_profiles == {"2": "high-quality", "3": "missing-profile"}


def test_default_alias_per_purpose():
    assert model_for(Purpose.CHAT) == "ats-chat"
    assert model_for(Purpose.SCREENING, client_id=1) == "ats-screening"


def test_client_profile_overrides_only_its_purposes():
    assert profile_of(2) == "high-quality"
    assert model_for(Purpose.CHAT, client_id=2) == "ats-chat-quality"
    assert model_for(Purpose.EXTRACTION, client_id=2) == "ats-extraction"     # not overridden -> default


def test_unknown_profile_and_embeddings_fall_back_to_defaults():
    assert model_for(Purpose.CHAT, client_id=3) == "ats-chat"                  # profile not defined
    assert model_for(Purpose.EMBEDDING, client_id=2) == "ats-embedding"        # never per client


def test_model_identity_carries_config_version():
    assert model_identity(Purpose.CHAT, 2) == "ats-chat-quality@7"


# ---------------------------------------------------------------- guarded calls
def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://gateway/v1/chat/completions")
    return openai.APIStatusError("upstream", response=httpx.Response(status, request=request), body=None)


async def test_guarded_returns_result():
    async def ok():
        return 42
    assert await guarded(Purpose.CHAT, ok, "test.ok") == 42


@pytest.mark.parametrize("error, spec", [
    (_status_error(503), Errors.LLM_UNAVAILABLE),
    (_status_error(429), Errors.LLM_UNAVAILABLE),
    (ValueError("broken json"), Errors.LLM_MALFORMED),
])
async def test_guarded_maps_failures_to_catalog_errors(error, spec):
    async def fail():
        raise error
    with pytest.raises(AppError) as raised:
        await guarded(Purpose.ANSWER, fail, f"test.{spec.code}")
    assert raised.value.spec is spec


async def test_guarded_times_out(monkeypatch):
    async def slow():
        await asyncio.sleep(1)
    with pytest.raises(AppError) as raised:
        await guarded(Purpose.EXTRACTION, slow, "test.slow", timeout_s=0.01)
    assert raised.value.spec is Errors.LLM_TIMEOUT


async def test_guarded_retries_a_malformed_reply_once():
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("broken json")
        return "ok"
    assert await guarded(Purpose.SCREENING, flaky, "test.flaky") == "ok" and len(attempts) == 2


async def test_guarded_does_not_swallow_client_errors():
    async def bad_request():
        raise _status_error(400)
    with pytest.raises(openai.APIStatusError):
        await guarded(Purpose.CHAT, bad_request, "test.400")


# ---------------------------------------------------------------- structured completion + embeddings
class City(BaseModel):
    city: str


class FakeGateway:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.embeddings = SimpleNamespace(create=self._embed)

    async def _chat(self, **request):
        self.requests.append(request)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"city": "London"}'))])

    async def _embed(self, **request):
        self.requests.append(request)
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[0.1, 0.2, 0.3, 0.4])
                                     for i, _ in enumerate(request["input"])])


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(calls, "gateway_client", lambda: fake)
    monkeypatch.setattr(embeddings, "gateway_client", lambda: fake)
    return fake


async def test_structured_completion_uses_client_alias_schema_and_tenant_tag(gateway):
    result = await structured_completion(Purpose.CHAT, "Where?", City, client_id=2)
    request = gateway.requests[0]
    assert result.city == "London"
    assert request["model"] == "ats-chat-quality" and request["user"] == "client-2"
    assert request["response_format"]["json_schema"]["schema"]["properties"]["city"]["type"] == "string"


async def test_embeddings_apply_task_prefixes_and_dimension(gateway):
    documents = await embed_documents(["Lives in London"])
    query = await embed_query("location")
    plain = await embed_text("Apache Kafka")
    inputs = [r["input"] for r in gateway.requests]
    assert inputs == [["search_document: Lives in London"], ["search_query: location"], ["Apache Kafka"]]
    assert documents == [[0.1, 0.2, 0.3]] and len(query) == 3 and len(plain) == 3     # truncated to LLM_EMBEDDING_DIM
    assert all(r["model"] == "ats-embedding" for r in gateway.requests)
