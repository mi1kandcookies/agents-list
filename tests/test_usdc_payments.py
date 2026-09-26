"""Circle Sepolia USDC, EIP-712 domain discovery, EIP-1559 fees, x402 payments."""
import secrets

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from chain import usdc as usdc_mod
from chain.client import eip1559_fees
from chain.config import CIRCLE_USDC_SEPOLIA, get_address
from chain.usdc import (
    authorization_typed_data, fallback_domain, get_usdc_domain, recover_authorization_signer,
)
from chain.x402 import build_challenge

TREASURY = "0x" + "7" * 40


# ── fakes ──────────────────────────────────────────────────────────────────
class _Call:
    def __init__(self, value):
        self.value = value

    def call(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeToken:
    def __init__(self, address, name="USDC", version="2", fail=False):
        self.address = address
        self.calls = 0
        outer = self

        class Fns:
            def name(self_inner):
                outer.calls += 1
                return _Call(RuntimeError("rpc down") if fail else name)

            def version(self_inner):
                return _Call(version)

        self.functions = Fns()


class FakeEth:
    def __init__(self, token=None, base_fee=None, priority=None, gas_price=5):
        self.token = token
        self.base_fee = base_fee
        self._priority = priority
        self.gas_price = gas_price

    def contract(self, address, abi):
        if self.token is None:
            raise RuntimeError("no rpc")
        return self.token

    def get_block(self, which):
        return {} if self.base_fee is None else {"baseFeePerGas": self.base_fee}

    @property
    def max_priority_fee(self):
        if isinstance(self._priority, Exception):
            raise self._priority
        return self._priority


class FakeW3:
    def __init__(self, eth):
        self.eth = eth


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    usdc_mod.clear_cache()
    for var in ("USDC_ADDRESS", "CHAIN_ID", "MAX_FEE_GWEI", "PRIORITY_FEE_GWEI", "PAYMENT_RECIPIENT"):
        monkeypatch.delenv(var, raising=False)
    yield
    usdc_mod.clear_cache()


# ── token + domain ─────────────────────────────────────────────────────────
def test_default_payment_token_is_circle_sepolia_usdc():
    assert get_address("USDC") == CIRCLE_USDC_SEPOLIA == "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"


def test_domain_falls_back_to_usdc_v2_without_rpc():
    d = get_usdc_domain()   # conftest points RPC_URL at an unroutable port
    assert (d["name"], d["version"], d["chainId"]) == ("USDC", "2", 11155111)
    assert d["verifyingContract"] == CIRCLE_USDC_SEPOLIA
    assert d["source"] == "fallback"


def test_domain_is_read_from_contract_and_cached():
    token = FakeToken(CIRCLE_USDC_SEPOLIA, name="USD Coin", version="7")
    w3 = FakeW3(FakeEth(token=token))
    d = get_usdc_domain(w3=w3)
    assert (d["name"], d["version"], d["source"]) == ("USD Coin", "7", "contract")
    get_usdc_domain(w3=w3)
    assert token.calls == 1                     # second call served from cache
    get_usdc_domain(w3=w3, refresh=True)
    assert token.calls == 2


def test_domain_read_failure_falls_back():
    w3 = FakeW3(FakeEth(token=FakeToken(CIRCLE_USDC_SEPOLIA, fail=True)))
    assert get_usdc_domain(w3=w3)["source"] == "fallback"


def test_domain_endpoint(client):
    body = client.get("/api/x402/domain").get_json()
    assert body["domain"]["name"] == "USDC" and body["domain"]["version"] == "2"
    assert body["domain"]["chainId"] == 11155111
    assert body["token"]["address"] == CIRCLE_USDC_SEPOLIA
    assert body["primaryType"] == "TransferWithAuthorization"
    assert [f["name"] for f in body["types"]["TransferWithAuthorization"]] == [
        "from", "to", "value", "validAfter", "validBefore", "nonce"]


def test_config_js_exposes_usdc(client):
    body = client.get("/config.js").get_data(as_text=True)
    assert CIRCLE_USDC_SEPOLIA in body
    assert "AGENTSLIST_USDC_DOMAIN" in body


def test_x402_challenge_uses_token_domain_and_caip2():
    ch = build_challenge(1.5, TREASURY, "agent-1", usdc_address=CIRCLE_USDC_SEPOLIA)
    assert ch["chain"]["network"] == "eip155:11155111"
    assert ch["permit"]["domain"]["name"] == "USDC"
    assert ch["permit"]["domain"]["version"] == "2"
    assert ch["price"]["amountMicro"] == 1_500_000


# ── signatures ─────────────────────────────────────────────────────────────
def _signed_permit(acct, to=TREASURY, value=2_000_000, domain=None):
    domain = domain or fallback_domain()
    permit = {"from": acct.address, "to": to, "value": value, "validAfter": 0,
              "validBefore": 9_999_999_999, "nonce": "0x" + secrets.token_hex(32)}
    signed = Account.sign_message(encode_typed_data(full_message=authorization_typed_data(permit, domain)),
                                  private_key=acct.key)
    permit.update(v=signed.v, r=hex(signed.r), s=hex(signed.s))
    return permit


def test_recover_authorization_signer():
    acct = Account.create()
    permit = _signed_permit(acct)
    assert recover_authorization_signer(permit, fallback_domain()) == acct.address
    other = dict(fallback_domain(), name="Mock USDC", version="1")
    assert recover_authorization_signer(permit, other) != acct.address


# ── EIP-1559 fees ──────────────────────────────────────────────────────────
GWEI = 10 ** 9


def test_eip1559_fees_from_base_fee_and_node_tip():
    fees = eip1559_fees(FakeW3(FakeEth(base_fee=10 * GWEI, priority=2 * GWEI)))
    assert fees == {"type": 2, "maxFeePerGas": 22 * GWEI, "maxPriorityFeePerGas": 2 * GWEI}


def test_eip1559_tip_fallback_and_cap(monkeypatch):
    fees = eip1559_fees(FakeW3(FakeEth(base_fee=10 * GWEI, priority=RuntimeError("unsupported"))))
    assert fees["maxPriorityFeePerGas"] == int(1.5 * GWEI)
    monkeypatch.setenv("MAX_FEE_GWEI", "15")
    capped = eip1559_fees(FakeW3(FakeEth(base_fee=10 * GWEI, priority=2 * GWEI)))
    assert capped["maxFeePerGas"] == 15 * GWEI


def test_legacy_gas_price_without_base_fee():
    assert eip1559_fees(FakeW3(FakeEth(base_fee=None, gas_price=7))) == {"gasPrice": 7}


# ── /api/x402/pay is closed, even with a facilitator configured ───────────
class FakeOnChain:
    def __init__(self):
        self.facilitator = Account.create()
        self.w3 = FakeW3(FakeEth(token=None))   # domain read fails -> fallback
        self.executed = []

    def x402_execute(self, payload):
        self.executed.append(payload)
        return {"status": "settled", "txHashes": {"permit": "0x" + "ab" * 32}}


@pytest.fixture()
def facilitator(monkeypatch):
    fake = FakeOnChain()
    monkeypatch.setenv("PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setattr("app.chain.routes.get_onchain", lambda: fake)
    return fake


def _pay_body(permit, agent_id):
    return {**permit, "value": str(permit["value"]), "agentId": agent_id, "task": "build it"}


@pytest.mark.parametrize("variant", ["valid", "bad_signature", "wrong_recipient"])
def test_pay_never_submits_or_records(client, db, agent, facilitator, variant):
    """A correctly signed permit used to settle with no approval; now nothing
    is submitted and no order or transaction is recorded, whatever the body."""
    from app.models import ChainTransaction, Order
    permit = _signed_permit(Account.create(), to="0x" + "9" * 40 if variant == "wrong_recipient"
                            else TREASURY)
    if variant == "bad_signature":
        permit["from"] = Web3.to_checksum_address("0x" + "b" * 40)
    resp = client.post("/api/x402/pay", json=_pay_body(permit, agent))
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_PAYMENT_DISABLED"
    assert facilitator.executed == []
    assert Order.query.count() == 0 and ChainTransaction.query.count() == 0
