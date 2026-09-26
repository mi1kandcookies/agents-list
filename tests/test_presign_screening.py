"""Payer-side Intercepta gate: no signer call before all three scans pass."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_account import Account

from app.screening.intercepta import (
    QUICK_SCAN_PATH, SCAN_MESSAGE_PATH, TOKEN_RISKS_PATH, InterceptaClient,
)
from app.screening.presign import InterceptaPreSignGate, PreSignScreeningError
from app.screening.service import MAINNET_USDC
from chain import x402_v2
from chain.config import CIRCLE_USDC_SEPOLIA
from tests.fakes.intercepta import FakeInterceptaHttp

FIXTURES = Path(__file__).parent / "fixtures" / "screening"
PAYEE = "0x" + "a1" * 20
MAINNET_PAYEE = "0x" + "b2" * 20
AMOUNT = 50_000
DOMAIN = {"name": "USDC", "version": "2", "chainId": 11155111,
          "verifyingContract": CIRCLE_USDC_SEPOLIA}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def typed_data(payer: str) -> dict:
    return {
        "types": {"EIP712Domain": [], "TransferWithAuthorization": [
            {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
        "primaryType": "TransferWithAuthorization", "domain": dict(DOMAIN),
        "message": {"from": payer, "to": PAYEE, "value": AMOUNT, "validAfter": 0,
                    "validBefore": 1_900_000_000, "nonce": "0x" + "01" * 32},
    }


def client_with(http):
    return InterceptaClient(api_key="test-key", base_url="https://intercepta.test",
                            http=http, timeout=4)


def route_clean(http):
    http.route("GET", QUICK_SCAN_PATH.format(address=MAINNET_PAYEE),
               body=fixture("quick_scan_clean"))
    http.route("GET", TOKEN_RISKS_PATH.format(address=MAINNET_USDC),
               body=fixture("token_usdc_info"))
    http.route("POST", SCAN_MESSAGE_PATH, body=fixture("signature_low"))


def test_clean_payment_is_allowed_only_after_all_scans():
    http = FakeInterceptaHttp()
    route_clean(http)
    payer = Account.create()
    gate = InterceptaPreSignGate(
        pay_to=PAYEE, amount_micro=AMOUNT, screening_address=MAINNET_PAYEE,
        client=client_with(http), payment_token=CIRCLE_USDC_SEPOLIA,
    )
    gate(typed_data(payer.address))
    assert [call["path"] for call in http.requests] == [
        QUICK_SCAN_PATH.format(address=MAINNET_PAYEE),
        TOKEN_RISKS_PATH.format(address=MAINNET_USDC),
        SCAN_MESSAGE_PATH,
    ]
    message = json.loads(http.requests[-1]["json"]["message"])
    assert message["message"]["nonce"] == "0x" + "01" * 32
    assert message["domain"]["chainId"] == 1
    assert message["domain"]["verifyingContract"] == MAINNET_USDC


def test_refused_payee_blocks_before_x402_signer():
    http = FakeInterceptaHttp()
    route_clean(http)
    http.route("GET", QUICK_SCAN_PATH.format(address=MAINNET_PAYEE),
               body=fixture("quick_scan_known_scammer"))
    payer = Account.create()
    gate = InterceptaPreSignGate(
        pay_to=PAYEE, amount_micro=AMOUNT, screening_address=MAINNET_PAYEE,
        client=client_with(http), payment_token=CIRCLE_USDC_SEPOLIA,
    )
    with pytest.raises(PreSignScreeningError) as exc:
        x402_v2.sign_payment(
            payer,
            x402_v2.build_requirements(pay_to=PAYEE, amount_micro=AMOUNT, domain=DOMAIN),
            expected=x402_v2.Expectation(pay_to=PAYEE, amount_micro=AMOUNT),
            domain=DOMAIN,
            before_sign=gate,
        )
    assert exc.value.code == "SCREENING_REFUSED"
    assert [call["path"] for call in http.requests] == [
        QUICK_SCAN_PATH.format(address=MAINNET_PAYEE),
        TOKEN_RISKS_PATH.format(address=MAINNET_USDC),
        SCAN_MESSAGE_PATH,
    ]


def test_missing_mainnet_mapping_is_a_hold():
    with pytest.raises(PreSignScreeningError) as exc:
        InterceptaPreSignGate(pay_to=PAYEE, amount_micro=AMOUNT,
                              address_map={}, client=client_with(FakeInterceptaHttp()))(
                                  typed_data("0x" + "c3" * 20))
    assert exc.value.code == "UNMAPPED_ADDRESS"
