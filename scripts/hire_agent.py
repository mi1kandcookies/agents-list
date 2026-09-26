#!/usr/bin/env python3
"""Hire one named specialist through ENS discovery and protected x402.

Example (credentials and the mainnet screening map stay outside the repo):

    python scripts/hire_agent.py \
      --agent quartz-qa-test-planner.agentslist-app.eth \
      --task-file demo/api-spec.txt \
      --max-usdc 0.10

The mandate is issued by the human-approval boundary. This runner only uses a
single-use mandate it was given; it cannot mint approval or sign until the
official x402 pre-payment hook has screened the exact authorization.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests
from eth_account import Account

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.screening.presign import InterceptaPreSignGate, PreSignScreeningError
from chain import x402_v2
from chain.x402_official import create_payment_payload, payment_signature_header
from chain.x402_v2 import Expectation, PaymentRequirements


def _micro(value: str) -> int:
    try:
        result = Decimal(value) * 1_000_000
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("must be a decimal USDC amount") from None
    if result < 0 or result != result.to_integral_value():
        raise argparse.ArgumentTypeError("must have at most six decimal places")
    return int(result)


def _json_response(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:1000]}
    return body if isinstance(body, dict) else {"value": body}


def _resolve(base_url: str, name: str, timeout: float) -> dict:
    response = requests.get(f"{base_url}/api/names/resolve",
                            params={"name": name}, timeout=timeout)
    body = _json_response(response)
    if not response.ok:
        raise RuntimeError(f"ENS name discovery failed ({response.status_code}): "
                           f"{body.get('error', 'unknown error')}")
    if body.get("status") != "active" or body.get("kind") != "agent":
        raise RuntimeError("ENS name is not an active agent record")
    records = body.get("records")
    if not isinstance(records, dict) or not records.get("payout"):
        raise RuntimeError("ENS agent has no x402 payout record")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, help="active ENS agent name")
    parser.add_argument("--task-file", required=True, help="buyer-owned API specification")
    parser.add_argument("--max-usdc", required=True, type=_micro,
                        help="maximum amount the intent permits")
    parser.add_argument("--base-url", default=os.environ.get("AGENTSLIST_BASE_URL",
                                                               "http://127.0.0.1:8090"))
    parser.add_argument("--screening-address",
                        help="mainnet address for the ENS payee; otherwise use SCREENING_ADDRESS_MAP")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    payer_key = os.environ.get("X402_PAYER_PRIVATE_KEY", "").strip()
    mandate = os.environ.get("X402_MANDATE_TOKEN", "").strip()
    if not payer_key or not mandate:
        parser.error("X402_PAYER_PRIVATE_KEY and X402_MANDATE_TOKEN are required")
    try:
        with open(args.task_file, encoding="utf-8") as task_file:
            task = task_file.read()
    except OSError as exc:
        parser.error(f"cannot read task file: {exc}")
    if not task.strip() or len(task) > 4000:
        parser.error("task file must contain between 1 and 4000 characters")

    base_url = args.base_url.rstrip("/")
    identity = _resolve(base_url, args.agent, args.timeout)
    agent_id = identity.get("agent_id")
    records = identity["records"]
    payout = str(records["payout"]).lower()
    endpoint = records.get("mcp") or f"{base_url}/api/agents/{quote(str(agent_id), safe='')}/tasks"

    first = requests.post(endpoint, json={"task": task}, timeout=args.timeout)
    required_body = _json_response(first)
    if first.status_code != 402:
        print(json.dumps({"stage": "requirements", "status": first.status_code,
                          "ens": identity, "body": required_body}, indent=2))
        return 1
    req = PaymentRequirements.from_dict((required_body.get("accepts") or [None])[0])
    if req.pay_to.lower() != payout:
        raise RuntimeError("x402 payTo differs from freshly resolved ENS payout; refusing")
    if req.amount_micro > args.max_usdc:
        raise RuntimeError("x402 amount exceeds the approved --max-usdc limit")

    payer = Account.from_key(payer_key)
    gate = InterceptaPreSignGate(
        pay_to=req.pay_to,
        amount_micro=req.amount_micro,
        screening_address=args.screening_address,
        payment_token=req.asset,
    )
    try:
        payload = create_payment_payload(
            required_body,
            payer,
            expected=Expectation(pay_to=payout, amount_micro=req.amount_micro,
                                 asset=req.asset, network=req.network),
            domain=None,
            before_sign=gate,
        )
    except PreSignScreeningError as exc:
        print(json.dumps({"stage": "pre_sign_screening", "status": "blocked",
                          "code": exc.code, "message": exc.message,
                          "ens_name": identity["name"],
                          "screening": exc.verdict}, indent=2))
        return 2

    second = requests.post(
        endpoint,
        json={"task": task},
        headers={"PAYMENT-SIGNATURE": payment_signature_header(payload),
                 "Authorization": f"Mandate {mandate}"},
        timeout=args.timeout,
    )
    result = _json_response(second)
    print(json.dumps({
        "stage": "result",
        "status": second.status_code,
        "decisions": ["ens_resolved", "terms_validated", "screened_before_sign", "signed",
                       "submitted"],
        "ens": {"name": identity["name"], "endpoint": endpoint, "payout": payout},
        "payment": {"network": req.network, "amount_atomic": req.amount,
                    "pay_to": req.pay_to, "payer": payer.address},
        "screening": gate.last_verdict.as_dict() if gate.last_verdict else None,
        "receipt": result.get("payment"),
        "deliverable": result.get("deliverable"),
        "body": result,
    }, indent=2))
    return 0 if second.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
