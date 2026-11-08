"""The compute agent's MCP endpoint only answers service tokens of allowed callers for aud=agent-compute, and only
for the tools in TOOL_POLICY. Tokens come from an in-memory issuer (no auth-service needed)."""
import pytest
from starlette.testclient import TestClient

from agent_compute import server
from agent_common.constants import JWT_AUDIENCE_COMPUTE
from agent_common.middleware import secure_mcp_app
from agent_common.security.testing import LocalTokenIssuer

ISSUER = LocalTokenIssuer()


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = secure_mcp_app(server._mcp_app, server.settings, JWT_AUDIENCE_COMPUTE, server.MCP_PATH,
                         verifier=ISSUER.verifier())
    return TestClient(app)


def call_tool(client: TestClient, token: str | None, tool: str = "execute_code"):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(server.MCP_PATH, headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {}}})


def test_health_needs_no_token(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_wrong_audience_and_user_tokens_are_rejected(client):
    assert call_tool(client, None).status_code == 401
    assert call_tool(client, ISSUER.service_token("ats-orchestrator", "agent-skill")).status_code == 401
    assert call_tool(client, ISSUER.user_token()).status_code == 401


def test_unknown_caller_and_tool_outside_the_policy_are_forbidden(client):
    assert call_tool(client, ISSUER.service_token("intruder", JWT_AUDIENCE_COMPUTE)).status_code == 403
    denied = call_tool(client, ISSUER.service_token("ats-orchestrator", JWT_AUDIENCE_COMPUTE), "drop_tables")
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "AUTH-FBD-0002"
