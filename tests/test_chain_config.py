"""chain.config: Sepolia defaults, env overrides, explorer_url()."""
import re

import pytest

from chain.config import (
    SEPOLIA_CHAIN_ID, explorer_url, get_address, get_addresses, get_chain_config,
    get_deployment,
)

CHAIN_ENV = ("CHAIN_ID", "CHAIN_NAME", "RPC_URL", "EXPLORER_URL", "NATIVE_CURRENCY_SYMBOL",
             "NATIVE_CURRENCY_NAME", "ESCROW_ADDRESS", "AGENT_REGISTRY_ADDRESS",
             "REPUTATION_ADDRESS", "STAKING_ADDRESS", "ERC8004_IDENTITY_REGISTRY",
             "PAYMENT_RECIPIENT", "PAYMENT_TOKEN_MODE", "MOCK_USDC_ADDRESS", "USDC_ADDRESS")
TX = "ab" * 32
ADDR = "0x" + "1" * 40


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in CHAIN_ENV:
        monkeypatch.delenv(var, raising=False)


def test_sepolia_defaults():
    cfg = get_chain_config()
    assert cfg.chain_id == SEPOLIA_CHAIN_ID == 11155111
    assert cfg.chain_id_hex == "0xaa36a7"
    assert cfg.caip2 == "eip155:11155111"
    assert cfg.name == "Ethereum Sepolia"
    assert cfg.explorer == "https://sepolia.etherscan.io"
    assert "sepolia" in cfg.rpc_url
    assert cfg.native_currency["symbol"] == "ETH"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CHAIN_ID", "31337")
    monkeypatch.setenv("CHAIN_NAME", "Anvil")
    monkeypatch.setenv("RPC_URL", "http://127.0.0.1:8545")
    monkeypatch.setenv("EXPLORER_URL", "http://explorer.local/")
    cfg = get_chain_config()
    assert (cfg.chain_id, cfg.name, cfg.rpc_url, cfg.explorer) == (
        31337, "Anvil", "http://127.0.0.1:8545", "http://explorer.local")


def test_invalid_chain_id_falls_back(monkeypatch):
    monkeypatch.setenv("CHAIN_ID", "not-a-number")
    assert get_chain_config().chain_id == SEPOLIA_CHAIN_ID


@pytest.mark.parametrize("kind, value, expected", [
    ("tx", "0x" + TX, "https://sepolia.etherscan.io/tx/0x" + TX),
    ("tx", TX, "https://sepolia.etherscan.io/tx/0x" + TX),
    ("address", ADDR, f"https://sepolia.etherscan.io/address/{ADDR}"),
    ("token", ADDR, f"https://sepolia.etherscan.io/token/{ADDR}"),
    ("block", 123, "https://sepolia.etherscan.io/block/123"),
])
def test_explorer_url(kind, value, expected):
    assert explorer_url(kind, value) == expected


def test_explorer_url_empty_and_invalid():
    assert explorer_url("tx", "") is None
    assert explorer_url("address", None) is None
    with pytest.raises(ValueError):
        explorer_url("wallet", ADDR)


def test_explorer_url_follows_env_and_base(monkeypatch):
    monkeypatch.setenv("EXPLORER_URL", "https://example.test/")
    assert explorer_url("tx", "0x" + TX) == "https://example.test/tx/0x" + TX
    assert explorer_url("tx", "0x" + TX, base="https://other.test") == "https://other.test/tx/0x" + TX


def test_contract_addresses():
    addrs = get_addresses(include_missing=True)
    assert addrs["IdentityRegistry"] == "0x8004A818BFB912233c491871b3d84c89A494BD9e"
    assert addrs["ReputationRegistry"] == "0x8004B663056A597Dffe9eCcC1965A193B7388713"
    for legacy in ("AgentRegistry", "ReputationContract", "StakingSlashing", "EscrowPayment"):
        assert addrs[legacy] is None
    assert "EscrowPayment" in get_deployment()["notDeployed"]


def test_mock_payment_token_is_opt_in(monkeypatch):
    mock = "0x" + "2" * 40
    monkeypatch.setenv("MOCK_USDC_ADDRESS", mock)
    assert get_address("USDC") == "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
    monkeypatch.setenv("PAYMENT_TOKEN_MODE", "mock")
    assert get_address("USDC") == mock
    assert get_address("MockUSDC") == mock
    assert get_deployment()["paymentTokenMode"] == "mock"


def test_invalid_payment_token_mode_falls_back_to_circle(monkeypatch):
    monkeypatch.setenv("PAYMENT_TOKEN_MODE", "not-a-mode")
    monkeypatch.setenv("MOCK_USDC_ADDRESS", "0x" + "2" * 40)
    assert get_address("USDC") == "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"


def test_invalid_address_override_is_ignored(monkeypatch):
    monkeypatch.setenv("ESCROW_ADDRESS", "0xnot-an-address")
    assert get_address("EscrowPayment") is None
    monkeypatch.setenv("ESCROW_ADDRESS", ADDR)
    assert get_address("EscrowPayment") == ADDR


LEGACY_STRINGS = re.compile(r"fuji|avalanche|avax|snowtrace|43113|0xa869", re.I)


def test_config_js_is_sepolia(client):
    body = client.get("/config.js").get_data(as_text=True)
    assert '"chainId": 11155111' in body
    assert '"chainIdHex": "0xaa36a7"' in body
    assert "sepolia.etherscan.io" in body
    assert not LEGACY_STRINGS.search(body)


def test_wallet_status_is_public_and_token_guarded(client, monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", "mcp-test-token")
    assert client.get("/api/wallet/status").status_code == 401
    response = client.get("/api/wallet/status", headers={"Authorization": "Bearer mcp-test-token"})
    assert response.status_code == 200
    body = response.get_json()
    assert "wallets" in body and "private_key" not in response.get_data(as_text=True)


def test_protocol_status_reports_fail_closed_integrations(client):
    body = client.get("/api/protocol/status").get_json()
    assert body["onchain"]["chainId"] == SEPOLIA_CHAIN_ID
    assert body["integrations"]["intercepta"]["fail_closed"] is True
    assert body["integrations"]["world_approval"]["backend_gate"] is True


def test_onchain_info(client):
    info = client.get("/api/onchain/info").get_json()
    assert info["chainId"] == 11155111
    assert info["caip2"] == "eip155:11155111"


@pytest.mark.parametrize("url", ["/", "/marketplace", "/how-it-works", "/active-jobs",
                                 "/past-jobs", "/seller/create", "/seller/earnings",
                                 "/admin/dashboard"])
def test_pages_have_no_legacy_chain_strings(client, url):
    assert not LEGACY_STRINGS.search(client.get(url).get_data(as_text=True))


def test_static_js_has_no_legacy_chain_strings(app):
    import pathlib
    for js in pathlib.Path(app.static_folder, "js").glob("*.js"):
        assert not LEGACY_STRINGS.search(js.read_text()), js.name


def test_legacy_contract_routes_degrade_to_503(client):
    assert client.get("/api/session/1").status_code == 503
    assert client.get("/api/session/1").get_json()["code"] == "NOT_DEPLOYED"
    # Cancelling moved funds with no approval; it is closed, not just undeployed.
    assert client.post("/api/session/1/cancel").status_code == 410
    assert client.post("/api/session/1/cancel").get_json()["code"] == "LEGACY_ESCROW_DISABLED"
    resp = client.post("/api/agents/register",
                       json={"wallet": ADDR, "name": "Agent X", "endpointURL": "https://x"})
    assert resp.status_code == 503
