"""The ENS setup helper is read-only unless an operator opts into writes."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "setup_agent_names.py"
    spec = importlib.util.spec_from_file_location("setup_agent_names", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


def test_default_action_only_reads_health(monkeypatch, capsys):
    module = _module()
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(200, {"ok": True, "mode": "dry_run", "root": "agentslist-app.eth"})

    monkeypatch.setattr(module.requests, "request", request)
    assert module.main(["--token", "dev"]) == 0
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", "http://127.0.0.1:8787/health")
    ]
    assert '"mode": "dry_run"' in capsys.readouterr().out


def test_live_write_requires_explicit_confirmation(monkeypatch, capsys):
    module = _module()
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(200, {"ok": True, "mode": "live", "root": "agentslist-app.eth"})

    monkeypatch.setattr(module.requests, "request", request)
    assert module.main(["--token", "dev", "--setup-root"]) == 2
    assert len(calls) == 1
    assert "--confirm-live" in capsys.readouterr().err


def test_dry_run_setup_posts_only_requested_records(monkeypatch):
    module = _module()
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/health"):
            return _Response(200, {"ok": True, "mode": "dry_run"})
        return _Response(200, {"status": "active", "name": "qa.agentslist-app.eth"})

    monkeypatch.setattr(module.requests, "request", request)
    assert module.main([
        "--token", "dev", "--setup-root", "--agent-label", "qa",
        "--agent-id", "AGT-Z48W-5F9X-8", "--payout-address", "0x" + "1" * 40,
        "--endpoint", "https://example.test/tasks",
    ]) == 0
    assert [method for method, _, _ in calls] == ["GET", "POST", "POST"]
    root_payload = calls[1][2]["json"]
    agent_payload = calls[2][2]["json"]
    assert root_payload == {"duration_days": 365}
    assert agent_payload["records"] == {
        "payout": "0x" + "1" * 40,
        "mcp": "https://example.test/tasks",
    }
