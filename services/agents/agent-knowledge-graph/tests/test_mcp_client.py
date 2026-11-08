"""Shared MCP client (agent_common.mcp_client): service token for the target, error-code mapping, no internal text
leaks. Tokens come from an in-memory issuer (LocalTokenIssuer), not a running auth-service."""
from types import SimpleNamespace

import agent_common.mcp_client as mcp_client
import pytest
from agent_common.constants import JWT_AUDIENCE_KNOWLEDGE_GRAPH
from agent_common.errors import AppError, Errors
from agent_common.knowledge.errors import KnowledgeErrors
from agent_common.mcp_client import McpTarget, McpToolClient
from agent_common.resilience import BreakerConfig
from agent_common.security import ServiceTokenProvider, verify_service_token
from agent_common.security.testing import LocalTokenIssuer

ISSUER = LocalTokenIssuer()


def tokens() -> ServiceTokenProvider:
    """Service tokens of "ats-test" signed by the in-memory issuer (stand-in for the client_credentials call)."""
    async def fetch(audience):
        return ISSUER.service_token("ats-test", audience), 300

    return ServiceTokenProvider(fetch)


def target(name: str) -> McpTarget:
    return McpTarget(name=name, url="http://knowledge/mcp", audience=JWT_AUDIENCE_KNOWLEDGE_GRAPH, timeout_s=5,
                     unavailable=KnowledgeErrors.SERVICE_UNAVAILABLE, timeout=KnowledgeErrors.GRAPH_TIMEOUT,
                     retry_attempts=1, breaker=BreakerConfig(100, 1))


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
    assert await McpToolClient(target("t1"), tokens()).call("search_knowledge", {}) == '{"ok": true}'
    assert await verify_service_token(ISSUER.verifier(), seen[0], JWT_AUDIENCE_KNOWLEDGE_GRAPH) == "ats-test"


async def test_known_error_code_is_re_raised_as_the_same_spec(monkeypatch):
    fake_transport(monkeypatch, text="KNW-INV-0002: The ontology is not valid. Fix it.", is_error=True)
    with pytest.raises(AppError) as error:
        await McpToolClient(target("t2"), tokens()).call("ingest_episode", {})
    assert error.value.spec is KnowledgeErrors.ONTOLOGY_INVALID


async def test_transport_failure_becomes_unavailable_without_internal_text(monkeypatch):
    fake_transport(monkeypatch, raises=ConnectionError("10.0.0.7 refused: secret-host"))
    with pytest.raises(AppError) as error:
        await McpToolClient(target("t3"), tokens()).call("search_knowledge", {})
    assert error.value.spec is KnowledgeErrors.SERVICE_UNAVAILABLE
    assert "secret-host" not in str(error.value)


async def test_refused_service_token_is_raised_as_is_without_calling_the_target(monkeypatch):
    seen = []
    fake_transport(monkeypatch, text='{"ok": true}', seen=seen)

    async def refused(audience):
        raise AppError(Errors.SERVICE_FORBIDDEN)

    with pytest.raises(AppError) as error:
        await McpToolClient(target("t4"), ServiceTokenProvider(refused)).call("search_knowledge", {})
    assert error.value.spec is Errors.SERVICE_FORBIDDEN and seen == []
