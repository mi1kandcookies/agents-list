from agentslist_mcp.client import AgentListClient
from app.common.agent_ids import generate_agent_id


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def json(self):
        return {"status": "ok"}


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse()


def test_mcp_client_uses_configured_api_boundary():
    session = FakeSession()
    client = AgentListClient("http://api.test", session=session)
    public_id = generate_agent_id()
    assert client.get_agent(public_id) == {"status": "ok"}
    method, url, kwargs = session.calls[0]
    assert method == "GET" and url == f"http://api.test/api/agents/public/{public_id}"


def test_mcp_client_rejects_unchecked_agent_identifier_before_network():
    session = FakeSession()
    client = AgentListClient("http://api.test", session=session)
    try:
        client.get_agent("1")
    except ValueError as exc:
        assert "invalid agent id" in str(exc)
    else:
        raise AssertionError("invalid public id was accepted")
    assert session.calls == []
