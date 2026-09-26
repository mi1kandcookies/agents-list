"""HTTP client for the Agent's List JSON API (docs/decisions/0001-custody-chain.md §7).

No wallet keys live in this package: the API holds custody, and every
money-moving call only *starts* an approval that a human completes on their
phone.

Config: ``MCP_API_BASE`` (default ``http://127.0.0.1:8090``) and
``MCP_API_TOKEN`` (sent as ``Authorization: Bearer <token>``).
"""
from __future__ import annotations

import os
from urllib.parse import quote

import requests

DEFAULT_API_BASE = "http://127.0.0.1:8090"
DEFAULT_TIMEOUT_SECONDS = 20


class AgentListAPIError(RuntimeError):
    """A non-2xx API response, or the API being unreachable.

    ``code``/``error``/``field`` carry the API's ``{error, code, field}`` body
    verbatim so tools can surface them unchanged.
    """

    def __init__(self, status: int | None, code: str, error: str, field: str | None = None, body=None):
        self.status = status
        self.code = code
        self.error = error
        self.field = field
        self.body = body
        super().__init__(f"{code}: {error}" if status is None else f"HTTP {status} {code}: {error}")


def _seg(value) -> str:
    """Quote one path segment so ids can't smuggle extra path components."""
    return quote(str(value), safe="")


class AgentListClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, *,
                 session=None, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        base = base_url if base_url is not None else os.environ.get("MCP_API_BASE", "")
        self.base_url = (base.strip() or DEFAULT_API_BASE).rstrip("/")
        self.token = (token if token is not None else os.environ.get("MCP_API_TOKEN", "")).strip()
        self.session = session or requests.Session()
        self.timeout = timeout

    def request(self, method: str, path: str, *, params=None, json=None, headers=None):
        hdrs = {"Accept": "application/json"}
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        hdrs.update(headers or {})
        if params:
            params = {k: v for k, v in params.items() if v is not None and v != ""}
        try:
            resp = self.session.request(method, self.base_url + path, params=params or None,
                                        json=json, headers=hdrs, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AgentListAPIError(None, "API_UNREACHABLE",
                                    f"could not reach {self.base_url}: {exc.__class__.__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not 200 <= resp.status_code < 300:
            if isinstance(body, dict) and body.get("code"):
                raise AgentListAPIError(resp.status_code, str(body["code"]),
                                        str(body.get("error") or body["code"]), body.get("field"), body)
            text = (getattr(resp, "text", "") or "")[:300]
            raise AgentListAPIError(resp.status_code, f"HTTP_{resp.status_code}", text or "request failed", None, body)
        if body is None:
            raise AgentListAPIError(resp.status_code, "BAD_RESPONSE", "API returned a non-JSON body")
        return body

    # --- §7 endpoints -----------------------------------------------------

    def search_agents(self, q: str = "", category: str | None = None, limit: int | None = None):
        return self.request("GET", "/api/agents", params={"q": q, "category": category, "limit": limit})

    def create_engagement(self, payload: dict) -> dict:
        return self.request("POST", "/api/engagements", json=payload)

    def get_engagement(self, engagement_id: str) -> dict:
        return self.request("GET", f"/api/engagements/{_seg(engagement_id)}")

    def hire(self, engagement_id: str, *, confirm_amount_usdc, flow: str = "device") -> dict:
        return self.request("POST", f"/api/engagements/{_seg(engagement_id)}/hire",
                            json={"flow": flow, "confirm_amount_usdc": confirm_amount_usdc})

    def submit_milestone(self, engagement_id: str, idx: int, evidence) -> dict:
        return self.request("POST", f"/api/engagements/{_seg(engagement_id)}/milestones/{int(idx)}/submit",
                            json={"evidence": evidence})

    def release_milestone(self, engagement_id: str, idx: int, *, flow: str = "device") -> dict:
        return self.request("POST", f"/api/engagements/{_seg(engagement_id)}/milestones/{int(idx)}/release",
                            json={"flow": flow})

    def subhire(self, engagement_id: str, payload: dict, *, mandate_token: str) -> dict:
        # Sub-hires authenticate with the mandate, not the bearer token (§5).
        return self.request("POST", f"/api/engagements/{_seg(engagement_id)}/subhire", json=payload,
                            headers={"Authorization": f"Mandate {mandate_token}"})

    def get_chain(self, engagement_id: str) -> dict:
        return self.request("GET", f"/api/engagements/{_seg(engagement_id)}/chain")

    def get_approval(self, approval_id: str) -> dict:
        return self.request("GET", f"/api/approvals/{_seg(approval_id)}")

    def cancel_approval(self, approval_id: str) -> dict:
        return self.request("POST", f"/api/approvals/{_seg(approval_id)}/cancel")


def default_client() -> AgentListClient:
    return AgentListClient()
