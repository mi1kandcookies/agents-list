"""Hosting behind a proxy and on serverless (docs/deploy/vercel.md): trusted
X-Forwarded-* headers, cookie flags, the World ID callback derived from an
allowlisted host, per-instance state, and the Vercel startup checks."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from flask import jsonify, request, url_for

from app import create_app
from app.approvals import service
from app.approvals.executors import EXECUTORS, ExecutionResult
from app.config import (_engine_options, config as config_map, default_config_name,
                        validate_serverless_config)
from app.identity.world import WorldClient, allowed_hosts, derive_redirect_uri

FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "agents.example",
             "X-Forwarded-For": "203.0.113.7"}
MANIFEST = {"manifest_hash": "0x" + "a" * 64}
PEM_ENV = "MANDATE_SIGNING_KEY"


def _probe_app(**overrides):
    app = create_app("testing", **overrides)

    @app.get("/_probe")
    def _probe():
        from app.engagements.routes import _same_origin_browser
        return jsonify(scheme=request.scheme, host=request.host, ip=request.remote_addr,
                       url=url_for("_probe", _external=True), same_origin=_same_origin_browser())
    return app


# ── ProxyFix ────────────────────────────────────────────────────────────────

def test_trusted_proxy_headers_set_scheme_host_client_ip_and_same_origin():
    app = _probe_app(TRUST_PROXY=True)
    body = app.test_client().get("/_probe", headers={**FORWARDED,
                                                     "Origin": "https://agents.example"}).get_json()
    assert body["scheme"] == "https" and body["host"] == "agents.example"
    assert body["url"] == "https://agents.example/_probe"
    assert body["ip"] == "203.0.113.7"
    assert body["same_origin"] is True


def test_proxy_headers_are_ignored_unless_trusted():
    app = _probe_app(TRUST_PROXY=False)
    body = app.test_client().get("/_probe", headers={**FORWARDED,
                                                     "Origin": "https://agents.example"}).get_json()
    assert body["scheme"] == "http" and body["host"] == "localhost"
    assert body["ip"] != "203.0.113.7"
    assert body["same_origin"] is False


def test_trust_proxy_defaults_on_only_on_vercel(monkeypatch):
    import importlib
    import app.config as cfg
    try:
        monkeypatch.delenv("TRUST_PROXY", raising=False)
        monkeypatch.delenv("VERCEL", raising=False)
        assert importlib.reload(cfg).Config.TRUST_PROXY is False
        monkeypatch.setenv("VERCEL", "1")
        assert importlib.reload(cfg).Config.TRUST_PROXY is True
    finally:
        monkeypatch.delenv("VERCEL", raising=False)
        importlib.reload(cfg)


# ── cookies ─────────────────────────────────────────────────────────────────

def test_session_cookie_flags_in_production():
    prod = config_map["production"]
    assert prod.SESSION_COOKIE_SECURE is True
    assert prod.SESSION_COOKIE_HTTPONLY is True
    assert prod.SESSION_COOKIE_SAMESITE == "Lax"
    assert prod.PREFERRED_URL_SCHEME == "https"
    assert prod.ENV_NAME == "production"


def test_session_cookie_header_is_secure_httponly_lax(app, db, world_idp):
    app.config["SESSION_COOKIE_SECURE"] = True
    resp = app.test_client().get("/login", base_url="https://agents.example")
    assert resp.status_code == 302
    cookie = resp.headers["Set-Cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie


# ── redirect URI derivation ─────────────────────────────────────────────────

def test_allowed_hosts_parsing():
    assert allowed_hosts(" A.example ,b.example:8443,, ") == {"a.example", "b.example:8443"}
    assert allowed_hosts("") == frozenset()


@pytest.mark.parametrize("host, expected", [
    ("preview.example", "https://preview.example/auth/world/callback"),
    ("PREVIEW.example", "https://preview.example/auth/world/callback"),
    ("evil.example", None),
    ("preview.example.evil.example", None),
    ("preview.example@evil.example", None),
    ("", None),
    (None, None),
])
def test_derive_redirect_uri_only_for_exact_allowlisted_hosts(host, expected):
    assert derive_redirect_uri(host, frozenset({"preview.example"})) == expected


def test_explicit_redirect_uri_wins_over_the_request_host(client, world_idp, monkeypatch):
    monkeypatch.setenv("WORLD_ALLOWED_HOSTS", "preview.example")
    with client.application.test_request_context("/", base_url="https://preview.example"):
        assert service.redirect_uri() == "http://localhost/auth/world/callback"


def _signin_query(resp) -> dict:
    assert resp.status_code == 302, resp.data
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}


def test_login_derives_callback_for_allowlisted_host_and_exchanges_with_it(
        app, db, human, world_idp, monkeypatch):
    monkeypatch.delenv("WORLD_REDIRECT_URI")
    monkeypatch.setenv("WORLD_ALLOWED_HOSTS", "preview.example")
    client = app.test_client()
    base = "https://preview.example"

    params = _signin_query(client.get("/login?next=/jobs", base_url=base))
    assert params["redirect_uri"] == "https://preview.example/auth/world/callback"

    code = world_idp.issue_code(nonce=params["nonce"])
    resp = client.get("/auth/world/callback", base_url=base,
                      query_string={"state": params["state"], "code": code})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/jobs")
    token_req = [r for r in world_idp.requests if r["url"].endswith("/token")][0]
    assert token_req["data"]["redirect_uri"] == "https://preview.example/auth/world/callback"


def test_login_refuses_hosts_outside_the_allowlist(app, db, world_idp, monkeypatch):
    monkeypatch.delenv("WORLD_REDIRECT_URI")
    monkeypatch.setenv("WORLD_ALLOWED_HOSTS", "preview.example")
    resp = app.test_client().get("/login", base_url="https://evil.example")
    assert resp.status_code == 200 and b"not configured" in resp.data.lower()


def test_approval_start_without_callback_reports_provider_error(app, db, world_idp, monkeypatch):
    monkeypatch.delenv("WORLD_REDIRECT_URI")
    monkeypatch.setitem(EXECUTORS, "manifest.publish",
                        lambda a, act: ExecutionResult(ok=True, summary="done"))
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    resp = app.test_client().get(f"/approvals/{approval.id}/start",
                                 base_url="https://evil.example")
    assert resp.status_code == 302 and "error=provider" in resp.headers["Location"]


# ── per-instance state ──────────────────────────────────────────────────────

def test_device_poll_keeps_slow_down_interval_across_instances(world_idp):
    first = WorldClient()
    start = first.device_authorize()
    world_idp.slow_down(start.device_code)
    slowed = first.poll_device(start.device_code, interval=start.interval)
    assert slowed.status == "slow_down" and slowed.interval == start.interval + 5

    # A fresh process has never seen this device code; the stored interval wins.
    other = WorldClient()
    world_idp.devices[start.device_code]["status"] = "authorization_pending"
    pending = other.poll_device(start.device_code, interval=slowed.interval)
    assert pending.status == "pending" and pending.interval == slowed.interval


def test_web_flow_state_lives_in_db_and_cookie_not_in_process(client, db, world_idp, monkeypatch):
    """A callback served by another instance (a fresh app extension cache)
    still completes: state and PKCE verifier are on the approval row, and
    the browser binding is in the signed cookie session."""
    monkeypatch.setitem(EXECUTORS, "manifest.publish",
                        lambda a, act: ExecutionResult(ok=True, summary="done"))
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _signin_query(client.get(f"/approvals/{approval.id}/start"))
    client.application.extensions.pop("world_client")  # drop per-process caches
    code = world_idp.issue_code(nonce=params["nonce"])
    resp = client.get("/auth/world/callback",
                      query_string={"state": params["state"], "code": code})
    assert resp.status_code == 302
    db.session.expire_all()
    assert service.get(approval.id).state == "consumed"


# ── serverless startup checks ───────────────────────────────────────────────

def test_default_config_is_production_on_vercel(monkeypatch):
    monkeypatch.delenv("FLASK_ENV")
    monkeypatch.setenv("VERCEL", "1")
    assert default_config_name() == "production"
    monkeypatch.setenv("FLASK_ENV", "development")
    assert default_config_name() == "development"
    monkeypatch.delenv("FLASK_ENV")
    monkeypatch.delenv("VERCEL")
    assert default_config_name() == "development"


class _FakeApp:
    def __init__(self, **config):
        self.config = config


def test_vercel_refuses_sqlite(monkeypatch):
    monkeypatch.setenv(PEM_ENV, "pem")
    with pytest.raises(RuntimeError, match="DATABASE_URL must point at Postgres"):
        validate_serverless_config(_FakeApp(SQLALCHEMY_DATABASE_URI="sqlite:///agents_list.db"))
    with pytest.raises(RuntimeError, match="DATABASE_URL must point at Postgres"):
        validate_serverless_config(_FakeApp(SQLALCHEMY_DATABASE_URI=""))


def test_vercel_requires_a_shared_mandate_key(monkeypatch):
    monkeypatch.delenv(PEM_ENV, raising=False)
    with pytest.raises(RuntimeError, match="MANDATE_SIGNING_KEY"):
        validate_serverless_config(_FakeApp(SQLALCHEMY_DATABASE_URI="postgresql+psycopg://h/d"))


def test_vercel_accepts_postgres_with_a_mandate_key(monkeypatch):
    monkeypatch.setenv(PEM_ENV, "pem")
    validate_serverless_config(_FakeApp(SQLALCHEMY_DATABASE_URI="postgresql+psycopg://h/d"))


def test_create_app_on_vercel_with_sqlite_fails_at_startup(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv(PEM_ENV, "pem")
    with pytest.raises(RuntimeError, match="Serverless configuration invalid"):
        create_app("development", SQLALCHEMY_DATABASE_URI="sqlite:///agents_list.db",
                   AUTO_MIGRATE=False)


def test_postgres_engine_options_only_on_vercel(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    assert _engine_options("postgresql+psycopg://h/d") == {}
    monkeypatch.setenv("VERCEL", "1")
    opts = _engine_options("postgresql+psycopg://h/d")
    assert opts["pool_pre_ping"] is True and opts["connect_args"] == {"prepare_threshold": None}
    assert _engine_options("sqlite:///x.db") == {}
