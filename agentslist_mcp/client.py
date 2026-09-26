"""Small HTTP client used by MCP tools; no wallet keys live in this package."""
from __future__ import annotations

import os

import requests


class AgentListAPIError(RuntimeError):
    pass


class AgentListClient:
    def __init__(self, base_url: str | None = None, *, session=None):
        self.base_url = (base_url or os.environ.get("MCP_BASE_URL", "http://127.0.0.1:8090")).rstrip("/")
        self.session = session or requests.Session()

    def request(self, method: str, path: str, **kwargs) -> dict:
        headers = dict(kwargs.pop("headers", {}) or {})
        api_key = os.environ.get("MCP_API_KEY", "").strip()
        if api_key:
            headers["X-Api-Key"] = api_key
        response = self.session.request(method, self.base_url + path, headers=headers, timeout=20, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:500]}
        if not response.ok:
            raise AgentListAPIError(f"HTTP {response.status_code}: {body}")
        return body

    def search_agents(self, query: str = "") -> dict:
        return self.request("GET", "/api/search", params={"q": query})

    def get_agent(self, agent_id: str) -> dict:
        return self.request("GET", f"/api/agents/{agent_id}")

    def create_engagement(self, payload: dict) -> dict:
        return self.request("POST", "/api/engagements", json=payload)

    def get_engagement(self, engagement_id: str) -> dict:
        return self.request("GET", f"/api/engagements/{engagement_id}")


def default_client() -> AgentListClient:
    return AgentListClient()
