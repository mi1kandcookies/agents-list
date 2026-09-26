"""app.screening: Intercepta client, fail-closed policy, hop screening and the
verdict API. No network: every HTTP call goes to FakeInterceptaHttp, which
serves JSON fixtures shaped per the provider's OpenAPI reference."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from app.screening import policy
from app.screening.intercepta import (
    DEEP_SCAN_PATH, QUICK_SCAN_PATH, SCAN_MESSAGE_PATH, TOKEN_RISKS_PATH, InterceptaClient,
    InterceptaError,
)
from app.screening.service import (
    MAINNET_USDC, InterceptaScreener, clear_token_cache, load_address_map, mainnet_typed_data,
    screen,
)
from tests.fakes.intercepta import FakeInterceptaHttp

FIXTURES = Path(__file__).parent / "fixtures" / "screening"
BASE = "https://intercepta.test"
KEY = "test-key"
SEPOLIA_PAYEE = "0x" + "a1" * 20
MAINNET_PAYEE = "0x" + "b2" * 20
SEPOLIA_PAYER = "0x" + "c3" * 20
MAINNET_PAYER = "0x" + "d4" * 20
SEPOLIA_USDC = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
ADDRESS_MAP = {SEPOLIA_PAYEE: MAINNET_PAYEE, SEPOLIA_PAYER: MAINNET_PAYER}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def quick(addr=MAINNET_PAYEE):
    return QUICK_SCAN_PATH.format(address=addr)


TOKEN = TOKEN_RISKS_PATH.format(address=MAINNET_USDC)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture()
def http():
    h = FakeInterceptaHttp()
    h.route("GET", quick(), body=fixture("quick_scan_clean"))
    h.route("GET", TOKEN, body=fixture("token_usdc_info"))
    h.route("POST", SCAN_MESSAGE_PATH, body=fixture("signature_low"))
    return h


@pytest.fixture()
def screener(app, http):
    client = InterceptaClient(api_key=KEY, base_url=BASE, timeout=4, http=http)
    return InterceptaScreener(client, address_map=ADDRESS_MAP)


def release(screener, **kw):
    kw.setdefault("chain_address", SEPOLIA_PAYEE)
    kw.setdefault("amount_micro", 5_000_000)
    return screener.screen(kw.pop("hop", "milestone.release"), **kw)


def transfer_typed_data(to=SEPOLIA_PAYEE, frm=SEPOLIA_PAYER, contract=SEPOLIA_USDC):
    return {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USDC", "version": "2", "chainId": 11155111, "verifyingContract": contract},
        "message": {"from": frm, "to": to, "value": 5_000_000, "validAfter": 0,
                    "validBefore": 1_900_000_000, "nonce": "0x" + "00" * 32},
    }


# ── client: documented request shapes ────────────────────────────────────────

def test_client_requests_match_the_documented_endpoints(http):
    client = InterceptaClient(api_key=KEY, base_url=BASE + "/", timeout=2.5, http=http)
    http.route("GET", DEEP_SCAN_PATH.format(address=MAINNET_PAYEE), body=fixture("quick_scan_clean"))
    assert client.quick_scan_address(MAINNET_PAYEE)["toxicScore"] == 0
    assert client.deep_scan_address(MAINNET_PAYEE)["traits"] == []
    assert client.scan_token(MAINNET_USDC)["action"] == "info"
    typed = transfer_typed_data()
    assert client.scan_message(owner=MAINNET_PAYER, typed_data=typed)["riskGroup"] == "Low"

    q, d, t, m = http.requests
    assert q["url"] == f"{BASE}/api/public/v2/extension/account/{MAINNET_PAYEE}/quick-scan"
    assert d["url"] == f"{BASE}/api/public/v2/extension/account/{MAINNET_PAYEE}/toxic-score"
    assert t["url"] == f"{BASE}/api/public/v2/extension/token-intelligence/token/{MAINNET_USDC}/risks"
    assert t["params"] == {"chainId": "1"}
    assert m["method"] == "POST" and m["path"] == "/api/public/v2/extension/analysis/signature"
    assert m["json"]["from"] == MAINNET_PAYER and m["json"]["chainId"] == "1"
    assert json.loads(m["json"]["message"]) == typed          # message is a JSON string
    for r in http.requests:
        assert r["headers"]["X-API-KEY"] == KEY and r["timeout"] == 2.5


@pytest.mark.parametrize("route, code", [
    ({"exc": requests.Timeout()}, "TIMEOUT"),
    ({"exc": requests.ConnectionError()}, "UNREACHABLE"),
    ({"status": 503, "body": {"message": "down"}}, "HTTP_ERROR"),
    ({"status": 403, "body": {"message": "Forbidden"}}, "HTTP_ERROR"),
    ({"text": "<html>gateway</html>"}, "BAD_RESPONSE"),
    ({"body": {"score": 3}}, "BAD_RESPONSE"),
    ({"body": {"toxicScore": "high", "traits": []}}, "BAD_RESPONSE"),
    ({"body": {"toxicScore": 1, "traits": [{"risk": 1}]}}, "BAD_RESPONSE"),
])
def test_client_errors(http, route, code):
    http.route("GET", quick(), **route)
    client = InterceptaClient(api_key=KEY, base_url=BASE, http=http)
    with pytest.raises(InterceptaError) as err:
        client.quick_scan_address(MAINNET_PAYEE)
    assert err.value.code == code


def test_client_without_key_sends_nothing(http, monkeypatch):
    monkeypatch.delenv("INTERCEPTA_API_KEY", raising=False)
    client = InterceptaClient(base_url=BASE, http=http)
    with pytest.raises(InterceptaError) as err:
        client.quick_scan_address(MAINNET_PAYEE)
    assert err.value.code == "MISSING_KEY" and http.requests == []


def test_client_reads_env(monkeypatch):
    monkeypatch.setenv("INTERCEPTA_API_KEY", "k")
    monkeypatch.setenv("SCREENING_TIMEOUT_SECONDS", "1.5")
    monkeypatch.delenv("INTERCEPTA_BASE_URL", raising=False)
    client = InterceptaClient()
    assert client.base_url == "https://api.web3antivirus.io" and client.timeout == 1.5


# ── policy table ─────────────────────────────────────────────────────────────

TH = policy.Thresholds()


def _decide_address(body):
    return policy.decide(policy.assess_address_scan(body, "quick-scan", TH), TH)


@pytest.mark.parametrize("score, traits, verdict", [
    (0, [], "PAY"),
    (29, [], "PAY"),
    (30, [], "CAP"),
    (59, [], "CAP"),
    (60, [], "ASK_HUMAN"),
    (79.5, [], "ASK_HUMAN"),
    (80, [], "REFUSE"),
    (0, ["sanction_address"], "REFUSE"),
    (0, ["known_scammer"], "REFUSE"),
    (0, ["initiator_scam_transactions"], "REFUSE"),
    (0, ["fake_phishing_transfer"], "REFUSE"),
    (0, ["fake_phishing_contract_communication"], "REFUSE"),
    (0, ["blacklist"], "REFUSE"),
    (0, ["rug_pull"], "REFUSE"),
    (0, ["mixer_transfers"], "ASK_HUMAN"),
    (0, ["non_kyc_transfers"], "ASK_HUMAN"),
    (0, ["suspicious_deployer"], "ASK_HUMAN"),
    (0, ["suspicious_dex_pair_deployer"], "ASK_HUMAN"),
    (0, ["sanction_address_communication"], "ASK_HUMAN"),
    (0, ["some_new_trait"], "ASK_HUMAN"),
    (45, ["mixer_transfers"], "ASK_HUMAN"),
    (10, ["mixer_transfers", "blacklist"], "REFUSE"),
])
def test_address_policy_table(score, traits, verdict):
    body = {"toxicScore": score, "traits": [
        {"name": n, "risk": 50, "txsCount": 1, "description": n} for n in traits]}
    decision = _decide_address(body)
    assert decision.verdict == verdict
    assert decision.cap_micro == (10_000_000 if verdict == "CAP" else None)
    assert not decision.fail_closed
    assert bool(decision.reasons) == (verdict != "PAY")


def test_traits_drive_the_decision_over_a_low_score():
    decision = _decide_address(fixture("quick_scan_mixer"))       # score 20, mixer + non-KYC
    assert decision.verdict == "ASK_HUMAN"
    assert [r["code"] for r in decision.reasons] == ["TRAIT_MIXER_TRANSFERS",
                                                     "TRAIT_NON_KYC_TRANSFERS"]


def test_score_reason_matches_contract_example():
    decision = _decide_address({"toxicScore": 72, "traits": []})
    assert decision.reasons == [{"code": "TOXIC_SCORE_HIGH", "message": "Toxic score 72 ≥ 60",
                                 "source": "quick-scan"}]


def test_thresholds_from_env(monkeypatch):
    monkeypatch.setenv("SCREENING_REFUSE_SCORE", "90")
    monkeypatch.setenv("SCREENING_ASK_SCORE", "70")
    monkeypatch.setenv("SCREENING_CAP_SCORE", "40")
    monkeypatch.setenv("SCREENING_CAP_USDC", "2.5")
    th = policy.Thresholds.from_env()
    assert (th.refuse_score, th.ask_score, th.cap_score, th.cap_micro) == (90, 70, 40, 2_500_000)
    monkeypatch.setenv("SCREENING_CAP_SCORE", "95")
    with pytest.raises(ValueError):
        policy.Thresholds.from_env()


@pytest.mark.parametrize("name, verdict", [
    ("token_usdc_info", "PAY"), ("token_warn", "ASK_HUMAN"), ("token_block", "REFUSE")])
def test_token_action_policy(name, verdict):
    assert policy.decide(policy.assess_token_scan(fixture(name)), TH).verdict == verdict


def test_unknown_token_action_asks_human():
    body = dict(fixture("token_usdc_info"), action="quarantine")
    assert policy.decide(policy.assess_token_scan(body), TH).verdict == "ASK_HUMAN"


@pytest.mark.parametrize("name, group, verdict, codes", [
    ("signature_low", "Low", "PAY", []),
    ("signature_medium", "Medium", "CAP", ["RISK_GROUP_MEDIUM"]),
    ("signature_high", "High", "ASK_HUMAN", ["RISK_GROUP_HIGH"]),
    ("signature_high_drainer", "High", "REFUSE",
     ["SIGNATURE_KNOWN_MALICIOUS", "SIGNATURE_WALLET_DRAINER", "RISK_GROUP_HIGH"]),
])
def test_signature_risk_group_mapping(name, group, verdict, codes):
    body = fixture(name)
    assert body["riskGroup"] == group
    decision = policy.decide(policy.assess_signature_scan(body), TH)
    assert decision.verdict == verdict
    assert [r["code"] for r in decision.reasons] == codes


@pytest.mark.parametrize("group", ["Critical", "Unknown", ""])
def test_unknown_risk_group_asks_human(group):
    body = dict(fixture("signature_low"), riskGroup=group)
    decision = policy.decide(policy.assess_signature_scan(body), TH)
    assert decision.verdict == "ASK_HUMAN"
    assert decision.reasons[0]["code"] == "RISK_GROUP_UNRECOGNISED"


def test_enforce_verdict():
    now = 1_000
    ok = {"verdict": "PAY", "expires_at": now + 10}
    policy.enforce_verdict(ok, 10**12, now=now)
    with pytest.raises(policy.ScreeningBlocked) as err:
        policy.enforce_verdict(ok, 1, now=now + 10)
    assert err.value.code == "SCREENING_EXPIRED"
    cap = {"verdict": "CAP", "cap_micro": 50}
    policy.enforce_verdict(cap, 50, now=now)
    with pytest.raises(policy.ScreeningBlocked, match="cap"):
        policy.enforce_verdict(cap, 51, now=now)
    with pytest.raises(policy.ScreeningBlocked):
        policy.enforce_verdict({"verdict": "CAP"}, 1, now=now)
    ask = {"verdict": "ASK_HUMAN"}
    with pytest.raises(policy.ScreeningBlocked):
        policy.enforce_verdict(ask, 1, now=now)
    policy.enforce_verdict(ask, 1, now=now, acknowledged=True)
    with pytest.raises(policy.ScreeningBlocked) as err:
        policy.enforce_verdict({"verdict": "REFUSE", "reasons": [{"message": "sanctioned"}]},
                               1, now=now, acknowledged=True)
    assert err.value.code == "SCREENING_REFUSED" and "sanctioned" in str(err.value)
    with pytest.raises(policy.ScreeningBlocked):
        policy.enforce_verdict({"verdict": "MAYBE"}, 1, now=now)


# ── service: hops, mapping, persistence ──────────────────────────────────────

def test_clean_release_pays_and_persists(screener, http, db):
    from app.models import Screening
    v = release(screener, engagement_id="ENG-000000000000")
    assert v["verdict"] == "PAY" and v["cap_micro"] is None and v["reasons"] == []
    assert v["id"].startswith("SCR-") and v["hop"] == "milestone.release"
    assert v["subject"] == {"chain_address": SEPOLIA_PAYEE, "screened_address": MAINNET_PAYEE,
                            "network": "eip155:1", "agent_id": None}
    assert v["signals"]["toxic_score"] == 0 and v["signals"]["traits"] == []
    assert v["signals"]["token_risks"][0]["action"] == "info"
    assert v["provider"] == "intercepta" and v["fail_closed"] is False
    assert v["expires_at"] - v["created_at"] == 300 and v["latency_ms"] >= 0
    # Only the mainnet address goes to the provider.
    assert [r["path"] for r in http.requests] == [quick(), TOKEN]

    row = db.session.get(Screening, v["id"])
    assert row.verdict == "PAY" and row.screened_address == MAINNET_PAYEE
    assert row.engagement_id == "ENG-000000000000"
    assert row.raw["quick-scan"] == fixture("quick_scan_clean")
    assert row.raw["scan-token"]["action"] == "info"


def test_known_scammer_refuses_with_reasons(screener, http, db):
    from app.models import Screening
    http.route("GET", quick(), body=fixture("quick_scan_known_scammer"))
    v = release(screener)
    assert v["verdict"] == "REFUSE" and not v["fail_closed"]
    codes = [r["code"] for r in v["reasons"]]
    assert {"TRAIT_KNOWN_SCAMMER", "TRAIT_FAKE_PHISHING_TRANSFER", "TOXIC_SCORE_CRITICAL"} <= set(codes)
    row = db.session.get(Screening, v["id"])
    assert row.toxic_score == 95 and row.traits == ["known_scammer", "fake_phishing_transfer"]
    assert row.reasons == v["reasons"]


def test_elevated_score_caps(screener, http):
    http.route("GET", quick(), body={"toxicScore": 45, "traits": []})
    v = release(screener)
    assert v["verdict"] == "CAP" and v["cap_micro"] == 10_000_000


def test_token_block_refuses_and_token_scan_is_cached(screener, http):
    http.route("GET", TOKEN, body=fixture("token_block"))
    assert release(screener)["verdict"] == "REFUSE"
    assert release(screener, hop="engagement.fund")["verdict"] == "REFUSE"
    assert len(http.calls_to("/risks")) == 1
    assert len(http.calls_to("/quick-scan")) == 2


def test_token_errors_are_not_cached(screener, http):
    http.route("GET", TOKEN, status=502, body={"message": "bad gateway"})
    assert release(screener)["fail_closed"] is True
    http.route("GET", TOKEN, body=fixture("token_usdc_info"))
    assert release(screener)["verdict"] == "PAY"
    assert len(http.calls_to("/risks")) == 2


def test_onboarding_uses_deep_scan_and_skips_token(screener, http):
    http.route("GET", DEEP_SCAN_PATH.format(address=MAINNET_PAYEE), body=fixture("quick_scan_mixer"))
    v = release(screener, hop="payee.onboard", amount_micro=0)
    assert v["verdict"] == "ASK_HUMAN"
    assert [r["path"] for r in http.requests] == [DEEP_SCAN_PATH.format(address=MAINNET_PAYEE)]
    assert {r["source"] for r in v["reasons"]} == {"deep-scan"}


@pytest.mark.parametrize("route, code", [
    ({"exc": requests.Timeout()}, "PROVIDER_TIMEOUT"),
    ({"exc": requests.ConnectionError()}, "PROVIDER_UNREACHABLE"),
    ({"status": 500, "body": {"message": "oops"}}, "PROVIDER_HTTP_ERROR_500"),
    ({"status": 503, "text": "Service Unavailable"}, "PROVIDER_HTTP_ERROR_503"),
    ({"status": 429, "body": {"message": "rate limited"}}, "PROVIDER_HTTP_ERROR_429"),
    ({"text": "<html>not json</html>"}, "PROVIDER_BAD_RESPONSE"),
    ({"body": {"unexpected": True}}, "PROVIDER_BAD_RESPONSE"),
    ({"body": ["toxicScore", 0]}, "PROVIDER_BAD_RESPONSE"),
])
def test_provider_failures_refuse_fail_closed(screener, http, db, route, code):
    from app.models import Screening
    http.route("GET", quick(), **route)
    v = release(screener)
    assert v["verdict"] == "REFUSE" and v["fail_closed"] is True
    assert v["reasons"][0]["code"] == code
    row = db.session.get(Screening, v["id"])
    assert row.fail_closed and row.raw["error"]["code"]
    assert http.calls_to("/risks") == []            # stop at the first failure


def test_missing_key_refuses_without_calling(app, http):
    client = InterceptaClient(api_key="", base_url=BASE, http=http)
    v = release(InterceptaScreener(client, address_map=ADDRESS_MAP))
    assert v["verdict"] == "REFUSE" and v["fail_closed"]
    assert v["reasons"][0]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert http.requests == []


def test_default_screen_fails_closed_without_key(app, monkeypatch):
    monkeypatch.delenv("INTERCEPTA_API_KEY", raising=False)
    monkeypatch.setenv("SCREENING_ADDRESS_MAP", json.dumps(ADDRESS_MAP))

    def no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)
    v = screen("engagement.fund", chain_address=SEPOLIA_PAYEE, amount_micro=1)
    assert v["verdict"] == "REFUSE" and v["reasons"][0]["code"] == "PROVIDER_NOT_CONFIGURED"


def test_unmapped_address_refuses(screener, http):
    v = release(screener, chain_address="0x" + "ee" * 20)
    assert v["verdict"] == "REFUSE" and v["fail_closed"]
    assert v["reasons"][0]["code"] == "UNMAPPED_ADDRESS"
    assert v["subject"]["screened_address"] is None and http.requests == []


@pytest.mark.parametrize("addr", ["", "0x1234", "not-an-address", None])
def test_invalid_address_refuses(screener, http, addr):
    v = release(screener, chain_address=addr)
    assert v["verdict"] == "REFUSE" and v["reasons"][0]["code"] == "INVALID_ADDRESS"
    assert http.requests == []


def test_unknown_hop_is_a_programming_error(screener):
    with pytest.raises(ValueError):
        release(screener, hop="teleport")


def test_agent_screening_address_wins(app, db, agent, http):
    from app.models import Agent
    row = db.session.get(Agent, agent)
    row.public_id = "AGT-0000-0000-0"
    row.screening_address = MAINNET_PAYEE.upper().replace("0X", "0x")
    db.session.commit()
    client = InterceptaClient(api_key=KEY, base_url=BASE, http=http)
    s = InterceptaScreener(client, address_map={})
    for ref in (agent, "AGT-0000-0000-0"):
        v = release(s, chain_address="0x" + "f0" * 20, agent_id=ref)
        assert v["verdict"] == "PAY" and v["subject"]["screened_address"] == MAINNET_PAYEE
        assert v["subject"]["agent_id"] == "AGT-0000-0000-0"


def test_address_map_file_and_inline(tmp_path, monkeypatch):
    path = tmp_path / "map.json"
    path.write_text(json.dumps({SEPOLIA_PAYEE.upper().replace("0X", "0x"): MAINNET_PAYEE}))
    assert load_address_map(str(path)) == {SEPOLIA_PAYEE: MAINNET_PAYEE}
    assert load_address_map(json.dumps(ADDRESS_MAP)) == ADDRESS_MAP
    monkeypatch.delenv("SCREENING_ADDRESS_MAP", raising=False)
    assert load_address_map() == {}
    with pytest.raises(ValueError):
        load_address_map('{"0x12": "0x34"}')


def test_bad_config_fails_closed(app, http, monkeypatch):
    client = InterceptaClient(api_key=KEY, base_url=BASE, http=http)
    monkeypatch.setenv("SCREENING_ADDRESS_MAP", "/nonexistent/map.json")
    v = release(InterceptaScreener(client))
    assert v["verdict"] == "REFUSE" and v["reasons"][0]["code"] == "SCREENING_CONFIG_INVALID"
    monkeypatch.delenv("SCREENING_ADDRESS_MAP")
    monkeypatch.setenv("SCREENING_ASK_SCORE", "sixty")
    v = release(InterceptaScreener(client, address_map=ADDRESS_MAP))
    assert v["verdict"] == "REFUSE" and v["fail_closed"] and http.requests == []


# ── signature scans ──────────────────────────────────────────────────────────

def test_signature_payload_is_rebuilt_for_mainnet(screener, http):
    typed = transfer_typed_data()
    original = json.loads(json.dumps(typed))
    v = release(screener, hop="signature", typed_data=typed)
    assert v["verdict"] == "PAY" and v["signals"]["risk_group"] == "Low"
    assert typed == original                        # caller's payload untouched

    (call,) = http.calls_to("/analysis/signature")
    assert call["json"]["chainId"] == "1" and call["json"]["from"] == MAINNET_PAYER
    sent = json.loads(call["json"]["message"])
    assert sent["domain"] == {"name": "USD Coin", "version": "2", "chainId": 1,
                              "verifyingContract": MAINNET_USDC}
    assert sent["message"]["to"] == MAINNET_PAYEE and sent["message"]["from"] == MAINNET_PAYER
    assert sent["message"]["value"] == 5_000_000
    assert sent["primaryType"] == "TransferWithAuthorization" and sent["types"] == typed["types"]


def test_mainnet_typed_data_leaves_unknown_contracts_and_addresses():
    typed = transfer_typed_data(contract="0x" + "99" * 20, frm="0x" + "77" * 20)
    out = mainnet_typed_data(typed, resolve=lambda a: ADDRESS_MAP.get(a.lower()),
                             payment_token=SEPOLIA_USDC)
    assert out["domain"]["chainId"] == 1 and out["domain"]["name"] == "USDC"
    assert out["domain"]["verifyingContract"] == "0x" + "99" * 20
    assert out["message"]["from"] == "0x" + "77" * 20 and out["message"]["to"] == MAINNET_PAYEE


@pytest.mark.parametrize("name, verdict", [
    ("signature_low", "PAY"), ("signature_medium", "CAP"),
    ("signature_high", "ASK_HUMAN"), ("signature_high_drainer", "REFUSE")])
def test_signature_hop_verdicts(screener, http, db, name, verdict):
    from app.models import Screening
    http.route("POST", SCAN_MESSAGE_PATH, body=fixture(name))
    v = release(screener, hop="signature", typed_data=transfer_typed_data())
    assert v["verdict"] == verdict
    row = db.session.get(Screening, v["id"])
    assert row.risk_group == fixture(name)["riskGroup"]
    assert row.raw["scan-message-request"]["message"]["domain"]["chainId"] == 1


def test_signature_hop_without_payload_refuses(screener, http):
    v = release(screener, hop="signature")
    assert v["verdict"] == "REFUSE" and v["reasons"][0]["code"] == "MISSING_TYPED_DATA"
    assert http.requests == []


def test_signature_scan_garbage_fails_closed(screener, http):
    http.route("POST", SCAN_MESSAGE_PATH, body={"riskGroup": "Low"})   # no detectors/addresses
    v = release(screener, hop="signature", typed_data=transfer_typed_data())
    assert v["verdict"] == "REFUSE" and v["reasons"][0]["code"] == "PROVIDER_BAD_RESPONSE"


# ── API and screener hook ────────────────────────────────────────────────────

def test_get_screening_api(client, screener, http):
    http.route("GET", quick(), body={"toxicScore": 72, "traits": []})
    v = release(screener, engagement_id=None)
    resp = client.get(f"/api/screening/{v['id']}")
    assert resp.status_code == 200
    assert resp.get_json() == json.loads(json.dumps(v))
    assert resp.get_json()["reasons"][0]["message"] == "Toxic score 72 ≥ 60"
    missing = client.get("/api/screening/SCR-000000000000")
    assert missing.status_code == 404 and missing.get_json()["code"] == "NOT_FOUND"


def test_app_screener_override(app, fake_screener):
    app.extensions["screener"] = fake_screener
    fake_screener.set(SEPOLIA_PAYEE, "ASK_HUMAN")
    v = screen("subhire.hop", chain_address=SEPOLIA_PAYEE, amount_micro=1)
    assert v["verdict"] == "ASK_HUMAN" and fake_screener.calls[0]["hop"] == "subhire.hop"
