"""Fake HTTP transport for app.screening.intercepta: serves canned responses
per (method, path) and records every request. Never touches the network.

    http = FakeInterceptaHttp()
    http.route("GET", "/api/public/v2/extension/account/0xabc/quick-scan", body={...})
    http.route("GET", "...", status=503, body={"error": "down"})
    http.route("GET", "...", exc=requests.Timeout())       # raise instead
    http.route("GET", "...", text="<html>oops")             # non-JSON body
Unrouted paths answer 404.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import requests


def make_response(url: str, status: int = 200, body=None, text: str | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = (text if text is not None else json.dumps(body)).encode()
    resp.headers["Content-Type"] = "text/html" if text is not None else "application/json"
    resp.url = url
    resp.encoding = "utf-8"
    return resp


class FakeInterceptaHttp:
    def __init__(self):
        self.routes: dict[tuple[str, str], dict] = {}
        self.requests: list[dict] = []

    def route(self, method: str, path: str, *, status: int = 200, body=None,
              text: str | None = None, exc: Exception | None = None) -> None:
        self.routes[(method.upper(), path)] = {"status": status, "body": body, "text": text, "exc": exc}

    def calls_to(self, suffix: str) -> list[dict]:
        return [r for r in self.requests if r["path"].endswith(suffix)]

    def _handle(self, method: str, url: str, **kwargs) -> requests.Response:
        path = urlsplit(url).path
        self.requests.append({"method": method, "url": url, "path": path, **kwargs})
        spec = self.routes.get((method, path))
        if spec is None:
            return make_response(url, 404, {"message": "Not Found"})
        if spec["exc"] is not None:
            raise spec["exc"]
        return make_response(url, spec["status"], spec["body"], spec["text"])

    def get(self, url, **kwargs):
        return self._handle("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._handle("POST", url, **kwargs)
