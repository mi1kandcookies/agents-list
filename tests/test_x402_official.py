"""Interoperability with the official x402 Python client."""
from __future__ import annotations

import pytest
from eth_account import Account

from chain import x402_v2
from chain.x402_official import create_payment_payload, payment_signature_header
from chain.x402_v2 import Expectation, PaymentRequirements


PAYEE = "0x" + "c" * 40
AMOUNT = 50_000
DOMAIN = {
    "name": "USDC",
    "version": "2",
    "chainId": 11155111,
    "verifyingContract": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
}


def _required(req: PaymentRequirements) -> dict:
    return x402_v2.payment_required(req, resource_url="/api/agents/AGT-X/tasks")


def test_official_sdk_payload_uses_exact_pre_sign_gate():
    account = Account.create()
    req = x402_v2.build_requirements(pay_to=PAYEE, amount_micro=AMOUNT, domain=DOMAIN)
    seen = []
    payload = create_payment_payload(
        _required(req),
        account,
        expected=Expectation(pay_to=PAYEE, amount_micro=AMOUNT, asset=req.asset),
        domain=DOMAIN,
        before_sign=lambda typed: seen.append(typed),
    )
    body = payload.model_dump(by_alias=True, exclude_none=True)
    assert body["x402Version"] == 2
    assert body["accepted"]["payTo"] == PAYEE
    assert body["payload"]["authorization"]["to"] == PAYEE
    assert seen and seen[0]["primaryType"] == "TransferWithAuthorization"
    assert payment_signature_header(payload)


def test_official_sdk_payload_verifies_with_resource_adapter():
    account = Account.create()
    req = x402_v2.build_requirements(pay_to=PAYEE, amount_micro=AMOUNT, domain=DOMAIN)
    payload = create_payment_payload(
        _required(req), account,
        expected=Expectation(pay_to=PAYEE, amount_micro=AMOUNT, asset=req.asset),
        domain=DOMAIN,
    )
    verified = x402_v2.verify_payment(
        payload.model_dump(by_alias=True, exclude_none=True), req, domain=DOMAIN,
    )
    assert verified.payer == account.address.lower()
    assert verified.pay_to == PAYEE
    assert verified.amount_micro == AMOUNT


def test_official_sdk_pre_creation_gate_rejects_changed_terms():
    account = Account.create()
    req = x402_v2.build_requirements(pay_to=PAYEE, amount_micro=AMOUNT, domain=DOMAIN)
    try:
        create_payment_payload(
            _required(req), account,
            expected=Expectation(pay_to=PAYEE, amount_micro=AMOUNT + 1, asset=req.asset),
            domain=DOMAIN,
        )
    except x402_v2.X402Error as exc:
        assert exc.code == "AMOUNT_MISMATCH"
    else:
        raise AssertionError("official pre-payment hook accepted a changed amount")


def test_official_exact_gate_runs_before_account_signer():
    raw = Account.create()

    class CountingAccount:
        address = raw.address
        sign_calls = 0

        def sign_message(self, message):
            self.sign_calls += 1
            return raw.sign_message(message)

    account = CountingAccount()
    req = x402_v2.build_requirements(pay_to=PAYEE, amount_micro=AMOUNT, domain=DOMAIN)

    def refuse(_typed):
        raise x402_v2.X402Error("SCREENING_REFUSED", "blocked by screening")

    with pytest.raises(x402_v2.X402Error) as exc:
        create_payment_payload(
            _required(req), account,
            expected=Expectation(pay_to=PAYEE, amount_micro=AMOUNT, asset=req.asset),
            domain=DOMAIN,
            before_sign=refuse,
        )
    assert exc.value.code == "SCREENING_REFUSED"
    assert account.sign_calls == 0
