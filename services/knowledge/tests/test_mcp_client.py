"""Shared MCP client (ats_common.mcp_client): token per call, error-code mapping, no internal text leaks."""
from types import SimpleNamespace

import ats_common.mcp_client as mcp_client
import jwt
import pytest
from ats_common.constants import JWT_AUDIENCE_KNOWLEDGE
from ats_common.errors import AppError
from ats_common.knowledge.errors import KnowledgeErrors
from ats_common.mcp_client import McpTarget, McpToolClient
from ats_common.resilience import BreakerConfig

SECRET = "s" * 40


def target(name: str) -> McpTarget:
    return McpTarget(name=name, url="http://knowledge/mcp", audience=JWT_AUDIENCE_KNOWLEDGE, caller="ats-test",
                     timeout_s=5, unavailable=KnowledgeErrors.SERVICE_UNAVAILABLE,
                     timeout=KnowledgeErrors.GRAPH_TIMEOUT, retry_attempts=1, breaker=BreakerConfig(100, 1))


def fake_transport(monkeypatch, *, text="", is_error=False, raises=None, seen=None):
    async def call(url, headers, timeout_s, connect_s, tool, args):
        if seen is not None:
            seen.append(headers["Authorization"].removeprefix("Bearer "))
        if raises:
            raise raises
        return SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=is_error)
    monkeypatch.setattr(mcp_client, "_call_tool", call)


async def test_success_returns_text_and_sends_a_service_token_for_the_target(monkeypatch):
    seen = []
    fake_transport(monkeypatch, text='{"ok": true}', seen=seen)
    assert await McpToolClient(target("t1"), SECRET).call("search_knowledge", {}) == '{"ok": true}'
    claims = jwt.decode(seen[0], SECRET, algorithms=["HS256"], audience=JWT_AUDIENCE_KNOWLEDGE)
    assert claims["sub"] == "ats-test" and claims["kind"] == "service"


async def test_known_error_code_is_re_raised_as_the_same_spec(monkeypatch):
    fake_transport(monkeypatch, text="KNW-INV-0002: The ontology is not valid. Fix it.", is_error=True)
    with pytest.raises(AppError) as error:
        await McpToolClient(target("t2"), SECRET).call("ingest_episode", {})
    assert error.value.spec is KnowledgeErrors.ONTOLOGY_INVALID


async def test_transport_failure_becomes_unavailable_without_internal_text(monkeypatch):
    fake_transport(monkeypatch, raises=ConnectionError("10.0.0.7 refused: secret-host"))
    with pytest.raises(AppError) as error:
        await McpToolClient(target("t3"), SECRET).call("search_knowledge", {})
    assert error.value.spec is KnowledgeErrors.SERVICE_UNAVAILABLE
    assert "secret-host" not in str(error.value)
