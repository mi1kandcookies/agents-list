import json
import os
import sys
from typing import Optional

import requests


BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
API_KEY = os.environ.get("API_KEY")
TIMEOUT = 20
CHECK_MODE = os.environ.get("CHECK_MODE", "demo").strip().lower()


class CheckFailure(Exception):
    pass


def _print(msg: str) -> None:
    print(msg, flush=True)


def expect_status(
    name: str,
    method: str,
    path: str,
    expected: int,
    *,
    json_body: Optional[dict] = None,
    headers: Optional[dict] = None,
):
    url = f"{BASE_URL}{path}"
    resp = requests.request(
        method=method,
        url=url,
        json=json_body,
        headers=headers or {},
        timeout=TIMEOUT,
    )
    if resp.status_code != expected:
        snippet = resp.text[:400]
        raise CheckFailure(
            f"{name}: expected {expected}, got {resp.status_code} @ {path}\n{snippet}"
        )
    _print(f"PASS {name} status={resp.status_code}")
    return resp


def require_json_field(name: str, resp: requests.Response, field: str):
    try:
        payload = resp.json()
    except json.JSONDecodeError as e:
        raise CheckFailure(f"{name}: response is not JSON ({e})")
    if field not in payload:
        raise CheckFailure(f"{name}: missing JSON field '{field}'")
    _print(f"PASS {name} field={field}")
    return payload


def main() -> int:
    _print(f"Running full-stack integration checks against {BASE_URL}")
    if CHECK_MODE not in {"demo", "prod"}:
        raise CheckFailure("CHECK_MODE must be 'demo' or 'prod'")
    _print(f"Check mode: {CHECK_MODE}")

    # ── Frontend pages ──────────────────────────────────────────────────────
    expect_status("page-home", "GET", "/", 200)
    expect_status("page-marketplace", "GET", "/marketplace", 200)
    expect_status("page-sim", "GET", "/sim", 200)
    expect_status("page-how-it-works", "GET", "/how-it-works", 200)
    if CHECK_MODE == "demo":
        expect_status("page-agent-detail", "GET", "/agent/1", 200)
        expect_status("page-checkout", "GET", "/checkout/1", 200)

    # ── Core backend health ─────────────────────────────────────────────────
    health = expect_status("api-health", "GET", "/api/health", 200)
    require_json_field("api-health-json", health, "status")
    ready = expect_status("api-ready", "GET", "/api/ready", 200)
    require_json_field("api-ready-json", ready, "status")
    backend = expect_status("api-backend-status", "GET", "/api/backend/status", 200)
    backend_payload = require_json_field("api-backend-status-json", backend, "onchain")

    # ── Public read APIs used by frontend ───────────────────────────────────
    agents_resp = expect_status("api-agents", "GET", "/api/agents", 200)
    agents_payload = require_json_field("api-agents-json", agents_resp, "agents")
    agents = agents_payload.get("agents") or []
    if CHECK_MODE == "demo" and not agents:
        raise CheckFailure("api-agents: expected at least one seeded agent")
    _print(f"PASS api-agents-count count={len(agents)}")

    if agents:
        agent_id = int(agents[0]["id"])
        expect_status("api-agent", "GET", f"/api/agents/{agent_id}", 200)
        expect_status("api-price", "GET", f"/api/price/{agent_id}", 200)
    expect_status("api-search", "GET", "/api/search?q=agent", 200)
    expect_status("api-onchain-info", "GET", "/api/onchain/info", 200)
    expect_status("api-sim-status", "GET", "/api/sim/status", 200)
    expect_status("api-sim-all-agents", "GET", "/api/sim/all-agents", 200)
    expect_status("api-sim-open-bids", "GET", "/api/sim/open-bids", 200)
    expect_status("api-sim-surge-top", "GET", "/api/sim/surge-top", 200)

    # ── Key write flows (safe demo ops) ─────────────────────────────────────
    expect_status(
        "api-sim-speed-valid",
        "POST",
        "/api/sim/speed",
        200,
        json_body={"tickRealSeconds": 2.0},
    )
    expect_status(
        "api-sim-live-mode-get",
        "GET",
        "/api/sim/live-mode",
        200,
    )
    expect_status(
        "api-sim-live-mode-post",
        "POST",
        "/api/sim/live-mode",
        200,
        json_body={"enabled": False},
    )
    expect_status(
        "api-sim-post-bid-valid",
        "POST",
        "/api/sim/post-bid",
        200,
        json_body={
            "tokenBudget": 1000,
            "maxPricePerToken": 0.01,
            "categoryId": 0,
            "minTier": 1,
            "expiresInSec": 900,
        },
    )

    # ── Validation contract checks (must reject bad requests) ───────────────
    expect_status(
        "api-x402-invalid-rejected",
        "POST",
        "/api/x402/pay",
        400,
        json_body={
            "from": "0x123",
            "to": "0x456",
            "value": 0,
            "validBefore": 1,
            "nonce": 0,
            "v": 27,
            "r": "0x0",
            "s": "0x0",
            "agentId": 9999,
        },
    )
    expect_status(
        "api-sim-speed-invalid-rejected",
        "POST",
        "/api/sim/speed",
        400,
        json_body={"tickRealSeconds": 0.01},
    )

    # ── Admin auth check ────────────────────────────────────────────────────
    # If API_KEY is set, route should reject missing key. If not set, route can proceed.
    api_key_enabled = bool((backend_payload.get("features") or {}).get("apiKeyEnabled"))
    admin_resp = requests.post(
        f"{BASE_URL}/admin/payouts/release-all",
        timeout=TIMEOUT,
    )
    if api_key_enabled:
        if admin_resp.status_code != 401:
            raise CheckFailure(
                f"admin-auth-check: expected 401 without X-Api-Key when API key auth is enabled, got {admin_resp.status_code}"
            )
        _print("PASS admin-auth-check status=401")
        if API_KEY:
            admin_ok = requests.post(
                f"{BASE_URL}/admin/payouts/release-all",
                headers={"X-Api-Key": API_KEY},
                timeout=TIMEOUT,
            )
            if admin_ok.status_code not in (200, 400, 404):
                raise CheckFailure(
                    f"admin-auth-with-key: expected 2xx/4xx app response, got {admin_ok.status_code}"
                )
            _print(f"PASS admin-auth-with-key status={admin_ok.status_code}")
        else:
            _print("SKIP admin-auth-with-key (API_KEY env not provided to checker)")
    else:
        if admin_resp.status_code not in (200, 400, 404):
            raise CheckFailure(
                f"admin-auth-check: expected 2xx/4xx when API_KEY unset, got {admin_resp.status_code}"
            )
        _print(f"PASS admin-auth-check status={admin_resp.status_code}")

    _print("Full-stack integration checks complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckFailure as e:
        _print(f"FAIL {e}")
        raise SystemExit(1)
