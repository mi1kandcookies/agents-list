"""x402 v2 exact scheme, payment policy, escrow settlement and ENS reads
(chain/x402_v2.py, chain/payment_policy.py, chain/ens_v2.py)."""
from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest
from eth_abi import encode
from eth_account import Account

from chain import x402_v2
from chain.config import CIRCLE_USDC_SEPOLIA
from chain.ens_v2 import (
    PAYOUT_RECORD_KEY, ENSResolutionError, UniversalResolver, dns_encode, namehash, text_calldata,
)
from chain.escrow import EscrowError, EscrowService
from chain.payment_policy import NonceRegistry, PaymentPolicy, PolicyError
from chain.x402_v2 import Expectation, X402Error

PAYEE = "0x" + "c" * 40
AMOUNT = 50_000
DOMAIN = {"name": "USDC", "version": "2", "chainId": 11155111,
          "verifyingContract": CIRCLE_USDC_SEPOLIA, "source": "contract"}
NOW = 1_800_000_000


@pytest.fixture()
def payer():
    return Account.create()


@pytest.fixture()
def req():
    return x402_v2.build_requirements(pay_to=PAYEE, amount_micro=AMOUNT, domain=DOMAIN)


def _expect(**kw):
    return Expectation(**{"pay_to": PAYEE, "amount_micro": AMOUNT, **kw})


def _code(excinfo):
    return excinfo.value.code


# ── requirements + headers ───────────────────────────────────────────────────
def test_requirements_shape(req):
    d = req.to_dict()
    assert d == {"scheme": "exact", "network": "eip155:11155111", "amount": "50000",
                 "asset": CIRCLE_USDC_SEPOLIA, "payTo": PAYEE, "maxTimeoutSeconds": 300,
                 "extra": {"name": "USDC", "version": "2"}}
    body = x402_v2.payment_required(req, resource_url="/api/agents/X/tasks")
    assert body["x402Version"] == 2 and body["accepts"] == [d]


def test_requirements_refuse_other_networks(monkeypatch):
    monkeypatch.setenv("CHAIN_ID", "1")
    with pytest.raises(X402Error) as e:
        x402_v2.build_requirements(pay_to=PAYEE, amount_micro=AMOUNT, domain=DOMAIN)
    assert _code(e) == "NETWORK_UNSUPPORTED"


def test_header_round_trip(req, payer):
    payload = x402_v2.sign_payment(payer, req, expected=_expect(), domain=DOMAIN)
    header = x402_v2.encode_header(payload)
    assert x402_v2.decode_header(header) == payload
    # URL-safe alphabet and missing padding are accepted too.
    urlsafe = base64.urlsafe_b64encode(base64.b64decode(header)).decode().rstrip("=")
    assert x402_v2.decode_header(urlsafe) == payload


def test_before_sign_hook_runs_before_the_signer(req, payer, monkeypatch):
    order = []
    original = x402_v2.GuardedSigner.sign_typed_data

    def hook(typed):
        order.append("screening")
        assert typed["message"]["to"] == PAYEE
        assert int(typed["message"]["value"]) == AMOUNT

    def wrapped(self, full_message, **kwargs):
        order.append("signer")
        return original(self, full_message, **kwargs)

    monkeypatch.setattr(x402_v2.GuardedSigner, "sign_typed_data", wrapped)
    x402_v2.sign_payment(payer, req, expected=_expect(), domain=DOMAIN,
                         before_sign=hook)
    assert order == ["screening", "signer"]


@pytest.mark.parametrize("value", ["", "not base64!", base64.b64encode(b"[1]").decode(),
                                   base64.b64encode(b"{nope").decode(), "A" * 9000])
def test_decode_rejects_garbage(value):
    with pytest.raises(X402Error) as e:
        x402_v2.decode_header(value)
    assert _code(e) == "MALFORMED_PAYMENT"


# ── preflight + guarded signer ───────────────────────────────────────────────
@pytest.mark.parametrize("change, expected, code", [
    ({"pay_to": "0x" + "d" * 40}, {}, "RECIPIENT_MISMATCH"),
    ({"amount": "50001"}, {}, "AMOUNT_MISMATCH"),
    ({"asset": "0x" + "e" * 40}, {}, "ASSET_MISMATCH"),
    ({"network": "eip155:1"}, {}, "NETWORK_MISMATCH"),
    ({"scheme": "upto"}, {}, "SCHEME_MISMATCH"),
    ({"max_timeout_seconds": 7200}, {}, "VALIDITY_WINDOW"),
    ({"max_timeout_seconds": 600}, {"max_timeout_seconds": 300}, "VALIDITY_WINDOW"),
    ({"extra": {"name": "USD Coin", "version": "2"}}, {}, "DOMAIN_MISMATCH"),
    ({"extra": {"name": "USDC", "version": "1"}}, {}, "DOMAIN_MISMATCH"),
])
def test_preflight_rejections(req, payer, change, expected, code):
    bad = replace(req, **change)
    with pytest.raises(X402Error) as e:
        x402_v2.sign_payment(payer, bad, expected=_expect(**expected), domain=DOMAIN)
    assert _code(e) == code


def test_preflight_uses_domain_read_from_contract(req, payer, monkeypatch):
    """The domain comes from chain/usdc.py; a token reporting another
    name/version makes the requirements unsignable."""
    monkeypatch.setattr("chain.usdc.get_usdc_domain",
                        lambda **kw: {**DOMAIN, "name": "Bridged USDC"})
    with pytest.raises(X402Error) as e:
        x402_v2.preflight(req, _expect())
    assert _code(e) == "DOMAIN_MISMATCH"
    monkeypatch.setattr("chain.usdc.get_usdc_domain", lambda **kw: dict(DOMAIN))
    assert x402_v2.preflight(req, _expect())["name"] == "USDC"


def _typed(payer, **msg):
    from chain.usdc import authorization_typed_data
    permit = {"from": payer.address, "to": PAYEE, "value": AMOUNT, "validAfter": 0,
              "validBefore": NOW + 60, "nonce": "0x" + "1" * 64, **msg}
    return authorization_typed_data(permit, DOMAIN)


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(primaryType="Permit"),
    lambda t: t["domain"].update(chainId=1),
    lambda t: t["domain"].update(verifyingContract="0x" + "e" * 40),
    lambda t: t["domain"].update(version="1"),
    lambda t: t["message"].update(to="0x" + "d" * 40),
    lambda t: t["message"].update(value=AMOUNT + 1),
    lambda t: t["message"].update(**{"from": "0x" + "9" * 40}),
    lambda t: t["message"].update(validBefore=NOW + 10_000),
    lambda t: t["message"].update(validBefore=NOW - 1),
    lambda t: t["message"].update(validAfter=NOW + 100),
])
def test_guarded_signer_refuses_unexpected_typed_data(payer, mutate):
    signer = x402_v2.GuardedSigner(payer, _expect(max_timeout_seconds=300), DOMAIN)
    typed = _typed(payer)
    signer.sign_typed_data(typed, now=NOW)            # the untouched data signs
    typed = _typed(payer)
    mutate(typed)
    with pytest.raises(X402Error) as e:
        signer.sign_typed_data(typed, now=NOW)
    assert _code(e) == "SIGNER_REFUSED"


# ── verify ───────────────────────────────────────────────────────────────────
def test_sign_verify_round_trip(req, payer):
    payload = x402_v2.sign_payment(payer, req, expected=_expect(), domain=DOMAIN, now=NOW)
    v = x402_v2.verify_payment(x402_v2.decode_header(x402_v2.encode_header(payload)), req,
                               domain=DOMAIN, now=NOW + 5)
    assert v.payer == payer.address.lower() and v.pay_to == PAYEE and v.amount_micro == AMOUNT
    assert v.permit["v"] in (27, 28) and v.nonce == payload["payload"]["authorization"]["nonce"]
    assert "0x" + v.typed_data["message"]["nonce"].hex() == v.nonce


def _signed(payer, req):
    return x402_v2.sign_payment(payer, req, expected=_expect(), domain=DOMAIN, now=NOW)


@pytest.mark.parametrize("mutate, code", [
    (lambda p: p.update(x402Version=1), "UNSUPPORTED_VERSION"),
    (lambda p: p["accepted"].update(amount="1"), "REQUIREMENTS_MISMATCH"),
    (lambda p: p["accepted"].update(payTo="0x" + "d" * 40), "REQUIREMENTS_MISMATCH"),
    (lambda p: p["accepted"].update(extra={"name": "X", "version": "2"}), "REQUIREMENTS_MISMATCH"),
    (lambda p: p["payload"]["authorization"].update(value="1"), "AMOUNT_MISMATCH"),
    (lambda p: p["payload"]["authorization"].update(to="0x" + "d" * 40), "RECIPIENT_MISMATCH"),
    (lambda p: p["payload"]["authorization"].update(nonce="0x12"), "MALFORMED_PAYMENT"),
    (lambda p: p["payload"].update(signature="0x00"), "MALFORMED_PAYMENT"),
    (lambda p: p["payload"]["authorization"].update(**{"from": "0x" + "9" * 40}), "INVALID_SIGNATURE"),
    (lambda p: p["payload"]["authorization"].update(nonce="0x" + "2" * 64), "INVALID_SIGNATURE"),
])
def test_verify_rejections(req, payer, mutate, code):
    payload = _signed(payer, req)
    mutate(payload)
    with pytest.raises(X402Error) as e:
        x402_v2.verify_payment(payload, req, domain=DOMAIN, now=NOW)
    assert _code(e) == code


def test_verify_rejects_expired_and_early(req, payer):
    payload = _signed(payer, req)
    with pytest.raises(X402Error) as e:
        x402_v2.verify_payment(payload, req, domain=DOMAIN, now=NOW + 301)
    assert _code(e) == "EXPIRED"
    with pytest.raises(X402Error) as e:
        x402_v2.verify_payment(payload, req, domain=DOMAIN, now=NOW - 1000)
    assert _code(e) == "VALIDITY_WINDOW"


def test_verify_uses_server_domain(req, payer):
    """A signature over another token domain does not verify here."""
    payload = _signed(payer, req)
    with pytest.raises(X402Error) as e:
        x402_v2.verify_payment(payload, req, domain={**DOMAIN, "name": "Other"}, now=NOW)
    assert _code(e) == "DOMAIN_MISMATCH"


# ── policy ───────────────────────────────────────────────────────────────────
def test_policy_limits():
    policy = PaymentPolicy(max_amount_micro=AMOUNT, allowed_payees={PAYEE.upper().replace("0X", "0x")})
    policy.check(amount_micro=AMOUNT, pay_to=PAYEE)
    with pytest.raises(PolicyError) as e:
        policy.check(amount_micro=AMOUNT + 1, pay_to=PAYEE)
    assert _code(e) == "MAX_AMOUNT_EXCEEDED"
    with pytest.raises(PolicyError) as e:
        policy.check(amount_micro=AMOUNT, pay_to="0x" + "d" * 40)
    assert _code(e) == "PAYEE_NOT_ALLOWED"
    narrow = PaymentPolicy(10**9).narrowed(max_amount_micro=5, allowed_payees=[PAYEE])
    assert narrow.max_amount_micro == 5 and narrow.allowed_payees == {PAYEE}
    assert PaymentPolicy(3, [PAYEE]).narrowed(max_amount_micro=10, allowed_payees=["0x" + "d" * 40]) \
        == PaymentPolicy(3, [])


def test_duplicate_nonce_rejected():
    nonces = NonceRegistry()
    nonce = "0x" + "ab" * 32
    nonces.claim(nonce)
    with pytest.raises(PolicyError) as e:
        nonces.claim(nonce.upper().replace("0X", "0x"))
    assert _code(e) == "DUPLICATE_NONCE"
    nonces.release(nonce)
    nonces.claim(nonce)
    with pytest.raises(PolicyError) as e:
        nonces.claim("0x1234")
    assert _code(e) == "INVALID_NONCE"


# ── escrow settlement ────────────────────────────────────────────────────────
def test_simulated_settlement_checks_recipient_and_ref(req, payer):
    escrow = EscrowService()
    v = x402_v2.verify_payment(_signed(payer, req), req, domain=DOMAIN, now=NOW)
    tx = x402_v2.settle(v, escrow=escrow, ref=v.nonce)
    assert tx.status == "simulated" and tx.tx_hash.startswith("sim-")
    with pytest.raises(EscrowError) as e:
        x402_v2.settle(v, escrow=escrow, ref=v.nonce)
    assert _code(e) == "ALREADY_SETTLED"
    with pytest.raises(EscrowError) as e:
        escrow.settle_authorization(v.permit, pay_to="0x" + "d" * 40)
    assert _code(e) == "WRONG_RECIPIENT"
    assert x402_v2.settle_response(v, tx)["transaction"] == tx.tx_hash


# ── ENS reads ────────────────────────────────────────────────────────────────
def test_dns_encode_and_calldata():
    assert dns_encode("a.bc.eth") == b"\x01a\x02bc\x03eth\x00"
    data = text_calldata("helper.agentslist-app.eth", PAYOUT_RECORD_KEY)
    assert data[:4].hex() == "59d1d43c"                 # text(bytes32,string)
    assert data[4:36] == namehash("helper.agentslist-app.eth")
    with pytest.raises(ENSResolutionError):
        dns_encode("bad..name.eth")


class _FakeUR(UniversalResolver):
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.calls = []

    def _call(self, name, data):
        self.calls.append((name, data))
        if isinstance(self.value, Exception):
            raise self.value
        return encode(["string"], [self.value])


def test_payout_record():
    assert _FakeUR("0x" + "C" * 40).payout_address("h.agentslist-app.eth") == PAYEE
    assert _FakeUR("").payout_address("h.agentslist-app.eth") is None
    with pytest.raises(ENSResolutionError) as e:
        _FakeUR("not-an-address").payout_address("h.agentslist-app.eth")
    assert _code(e) == "ENS_BAD_RECORD"
    with pytest.raises(ENSResolutionError):
        _FakeUR(ENSResolutionError("rpc down")).payout_address("h.agentslist-app.eth")


def test_resolver_rpc_failure_is_an_error():
    ur = UniversalResolver(rpc_url="http://127.0.0.1:9", timeout=1)
    with pytest.raises(ENSResolutionError) as e:
        ur.payout_address("h.agentslist-app.eth")
    assert _code(e) == "ENS_UNAVAILABLE"


def test_payload_json_is_compact_and_sorted(req, payer):
    payload = _signed(payer, req)
    raw = base64.b64decode(x402_v2.encode_header(payload)).decode()
    assert raw == json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert int(payload["payload"]["authorization"]["validBefore"]) == NOW + 300
