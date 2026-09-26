from agentslist_mcp.client import AgentListClient


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
    assert client.get_engagement("ENG-1") == {"status": "ok"}
    method, url, kwargs = session.calls[0]
    assert method == "GET" and url == "http://api.test/api/engagements/ENG-1"
