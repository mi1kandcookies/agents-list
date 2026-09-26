"""HTTP client for the names sidecar (``ens/``, docs/decisions/0001-custody-chain.md §10).

Every failure (unconfigured, network, timeout, non-2xx, bad JSON) surfaces as
``SidecarError`` so callers can record it and move on; names are never fatal.
"""
from __future__ import annotations

import os

import requests
from flask import current_app, has_app_context


class SidecarError(Exception):
    def __init__(self, message: str, code: str = "SIDECAR_ERROR", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class SidecarClient:
    def __init__(self, base_url: str | None, token: str | None, timeout: float = 15.0,
                 session: requests.Session | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _request(self, method: str, path: str, *, json: dict | None = None,
                 params: dict | None = None) -> dict:
        if not self.configured:
            raise SidecarError("names sidecar is not configured", "NOT_CONFIGURED")
        try:
            resp = self.session.request(
                method, self.base_url + path, json=json, params=params, timeout=self.timeout,
                headers={"X-Sidecar-Token": self.token},
            )
        except requests.RequestException as exc:
            raise SidecarError(f"names sidecar unreachable: {exc.__class__.__name__}", "UNREACHABLE") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not resp.ok or not isinstance(body, dict):
            error = body.get("error") if isinstance(body, dict) else None
            code = body.get("code") if isinstance(body, dict) else None
            raise SidecarError(error or f"names sidecar returned HTTP {resp.status_code}",
                               code or "HTTP_ERROR", resp.status_code)
        return body

    def health(self) -> dict:
        return self._request("GET", "/health")

    def create_agent(self, *, agent_public_id: str, label: str, records: dict) -> dict:
        return self._request("POST", "/names/agent", json={
            "agent_public_id": agent_public_id, "label": label, "records": records})

    def create_job(self, *, parent: str, label: str, expiry: int, records: dict,
                   grantee: str | None = None) -> dict:
        return self._request("POST", "/names/job", json={
            "parent": parent, "label": label, "expiry": expiry, "records": records, "grantee": grantee})

    def create_subjob(self, *, parent: str, label: str, expiry: int, records: dict) -> dict:
        return self._request("POST", "/names/subjob", json={
            "parent": parent, "label": label, "expiry": expiry, "records": records})

    def revoke(self, name: str, grantee: str | None = None) -> dict:
        return self._request("POST", "/names/revoke", json={"name": name, "grantee": grantee})

    def tree(self, root: str | None = None) -> dict:
        return self._request("GET", "/names/tree", params={"root": root} if root else None)


def get_client() -> SidecarClient:
    """The app's sidecar client; tests install a fake at ``app.extensions["names_client"]``."""
    if has_app_context():
        override = current_app.extensions.get("names_client")
        if override is not None:
            return override
        cfg = current_app.config
        return SidecarClient(cfg.get("ENS_SIDECAR_URL"), cfg.get("ENS_SIDECAR_TOKEN"),
                             float(cfg.get("ENS_SIDECAR_TIMEOUT") or 15))
    return SidecarClient(os.environ.get("ENS_SIDECAR_URL"), os.environ.get("ENS_SIDECAR_TOKEN"))
