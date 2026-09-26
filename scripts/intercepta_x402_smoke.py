#!/usr/bin/env python3
"""Run the payer-side Intercepta gate before an x402 task payment.

Required environment variables (never commit them):
    INTERCEPTA_API_KEY       provider key
    X402_PAYER_PRIVATE_KEY   dedicated Sepolia demo payer
    X402_MANDATE_TOKEN       mandate granted to that payer agent

The first request obtains x402 requirements. Intercepta then scans the
mainnet representation of the payee, mainnet USDC, and the exact EIP-712
authorization before this script calls the local signer. A blocked scan exits
without sending a payment request.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from eth_account import Account

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.screening.presign import InterceptaPreSignGate, PreSignScreeningError
from chain import x402_v2
from chain.x402_v2 import Expectation, PaymentRequirements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="protected /api/agents/<AGT>/tasks URL")
    parser.add_argument("--task", required=True, help="task text sent to the specialist")
    parser.add_argument("--screening-address", required=True,
                        help="mainnet address used by Intercepta for this Sepolia payee")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()

    payer_key = os.environ.get("X402_PAYER_PRIVATE_KEY", "").strip()
    mandate = os.environ.get("X402_MANDATE_TOKEN", "").strip()
    if not payer_key or not mandate:
        parser.error("X402_PAYER_PRIVATE_KEY and X402_MANDATE_TOKEN are required")

    first = requests.post(args.url, json={"task": args.task}, timeout=args.timeout)
    if first.status_code != 402:
        print(json.dumps({"stage": "requirements", "status": first.status_code,
                          "body": first.json()}, indent=2))
        return 1
    required = first.json()
    req = PaymentRequirements.from_dict((required.get("accepts") or [None])[0])
    payer = Account.from_key(payer_key)
    gate = InterceptaPreSignGate(
        pay_to=req.pay_to,
        amount_micro=req.amount_micro,
        screening_address=args.screening_address,
        payment_token=req.asset,
    )
    try:
        payload = x402_v2.sign_payment(
            payer,
            req,
            expected=Expectation(pay_to=req.pay_to, amount_micro=req.amount_micro,
                                 asset=req.asset, network=req.network),
            before_sign=gate,
        )
    except PreSignScreeningError as exc:
        print(json.dumps({"stage": "pre_sign_screening", "status": "blocked",
                          "code": exc.code, "message": exc.message,
                          "verdict": exc.verdict}, indent=2))
        return 2

    response = requests.post(
        args.url,
        json={"task": args.task},
        headers={"X-PAYMENT": x402_v2.encode_header(payload),
                 "Authorization": f"Mandate {mandate}"},
        timeout=args.timeout,
    )
    try:
        body = response.json()
    except ValueError:
        body = response.text[:1000]
    print(json.dumps({"stage": "payment", "status": response.status_code,
                      "payer": payer.address, "pay_to": req.pay_to,
                      "amount_atomic": req.amount, "screening": gate.last_verdict.as_dict()
                      if gate.last_verdict else None, "body": body}, indent=2))
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
