#!/usr/bin/env python3
"""Create/resume one protected named-agent hire.

The private buyer key is read only by this process. It is never sent to Flask
or included in an LLM prompt. Without approval, the script stops before the
x402 client asks the signer to create a payment payload.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests


def _post(session, url, **kwargs):
    response = session.post(url, timeout=20, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {"error": response.text[:500]}
    if response.status_code >= 400 and response.status_code != 402:
        raise RuntimeError(f"HTTP {response.status_code}: {body}")
    return response, body


def _get(session, url):
    response = session.get(url, timeout=20)
    try:
        body = response.json()
    except ValueError:
        body = {"error": response.text[:500]}
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {body}")
    return response, body


def _print_trace(intent, *, approval=None, screening=None, receipt=None):
    print(json.dumps({
        "intentId": intent.get("id"), "status": intent.get("status"),
        "specialist": intent.get("specialistName"), "endpoint": intent.get("approvedEndpoint"),
        "payTo": intent.get("payTo"), "amountAtomic": intent.get("amountAtomic"),
        "taskHash": intent.get("taskHash"), "intentHash": intent.get("intentHash"),
        "approval": approval or intent.get("approval"),
        "screening": screening or intent.get("screening"), "receipt": receipt,
    }, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Hire one ENS-named QA specialist")
    parser.add_argument("--agent", help="ENSv2 name, e.g. qa.example.eth")
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--max-usdc", type=float, default=0.10)
    parser.add_argument("--base-url", default=os.environ.get("AGENTSLIST_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--intent-id", help="resume an existing approved intent")
    parser.add_argument("--owner-id", default=os.environ.get("HIRE_OWNER_ID", "local-owner"))
    parser.add_argument("--payer", default=os.environ.get("BUYER_ADDRESS", ""))
    parser.add_argument("--approve-local", action="store_true",
                        help="use the localhost-only approval adapter")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    session = requests.Session()

    if args.intent_id:
        response, intent = _get(session, f"{base}/api/hiring/intents/{args.intent_id}")
    else:
        if not args.agent or not args.task_file:
            parser.error("--agent and --task-file are required when creating an intent")
        task = args.task_file.read_text(encoding="utf-8")
        payer = args.payer
        if not payer:
            if os.environ.get("HIRE_PAYMENT_MODE") == "mock":
                payer = "0x1111111111111111111111111111111111111111"
            else:
                raise SystemExit("BUYER_ADDRESS is required for a real payment")
        response, intent = _post(session, f"{base}/api/hiring/intents", json={
            "agent": args.agent, "task": task, "maxUsdc": args.max_usdc,
            "ownerId": args.owner_id, "payer": payer,
        })

    if intent.get("status") not in {"approved", "payment_pending", "payment_confirmed", "delivered"}:
        _print_trace(intent)
        approval = intent.get("approval") or {}
        if args.approve_local and approval.get("approvalId"):
            _, decision = _post(session, f"{base}/api/hiring/approvals/{approval['approvalId']}/decision",
                                json={"ownerId": args.owner_id, "state": "approved"})
            print(json.dumps({"approvalDecision": decision}, indent=2, sort_keys=True))
            _, intent = _get(session, f"{base}/api/hiring/intents/{intent['id']}")
        else:
            print("Approval is required before a signer can be invoked.", file=sys.stderr)
            return 2

    work_url = f"{base}/api/hiring/intents/{intent['id']}/work"
    # The first request obtains official x402 requirements; it does not sign.
    response, body = _post(session, work_url, json={})
    if response.status_code != 402:
        _print_trace(body.get("intent", body), screening=body.get("screening"),
                     receipt=body.get("receipt"))
        return 0 if response.ok else 1

    if os.environ.get("HIRE_PAYMENT_MODE") == "mock":
        response, body = _post(session, work_url, headers={"X-Hire-Demo-Payment": "1"}, json={})
        _print_trace(body.get("intent", intent), approval=body.get("approval"),
                     screening=body.get("screening"), receipt=body.get("receipt"))
        print(json.dumps({"result": body.get("result")}, indent=2, sort_keys=True))
        return 0 if response.ok else 1

    private_key = os.environ.get("BUYER_PRIVATE_KEY", "").strip()
    if not private_key:
        raise SystemExit("BUYER_PRIVATE_KEY is required after approval for a real payment")
    from eth_account import Account
    from chain.payment_policy import PaymentPolicyError, validate_requirements
    from chain.x402_v2 import make_buyer_client
    account = Account.from_key(private_key)
    if account.address.lower() != intent["payer"].lower():
        raise SystemExit("BUYER_PRIVATE_KEY address does not match the approved payer")
    claim_response, claim_body = _post(session, f"{base}/api/hiring/intents/{intent['id']}/claim", json={})
    attempt_id = claim_body["attemptId"]

    def before_payment(context):
        # This hook runs before ExactEvmScheme creates any signature. The API
        # re-resolves ENS, re-screens the payee, and re-validates approval.
        preflight = session.get(f"{base}/api/hiring/intents/{intent['id']}/preflight", timeout=20)
        if preflight.status_code >= 400:
            from x402.schemas import AbortResult
            return AbortResult(f"preflight rejected: {preflight.text[:180]}")
        validate_requirements(type("Intent", (), {
            "network": intent["network"], "token_address": intent["tokenAddress"],
            "pay_to": intent["payTo"], "amount_atomic": int(intent["amountAtomic"]),
        })(), context.selected_requirements)
        return None

    client = make_buyer_client(type("Intent", (), {
        "token_address": intent["tokenAddress"], "payer": intent["payer"],
        "pay_to": intent["payTo"], "amount_atomic": int(intent["amountAtomic"]),
        "chain_id": int(intent["chainId"]),
    })(), account, before_payment_hook=before_payment)
    from x402.http import x402HTTPClientSync
    http_client = x402HTTPClientSync(client)
    headers, _payload = http_client.handle_402_response(
        {key: value for key, value in response.headers.items()}, response.content, work_url)
    headers["X-Hire-Attempt"] = attempt_id
    response, body = _post(session, work_url, headers=headers, json={})
    _print_trace(body.get("intent", intent), approval=body.get("approval"),
                 screening=body.get("screening"), receipt=body.get("receipt"))
    if body.get("result") is not None:
        print(json.dumps({"result": body["result"]}, indent=2, sort_keys=True))
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
