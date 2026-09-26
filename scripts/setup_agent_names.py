#!/usr/bin/env python3
"""Check or explicitly initialize the local ENS names sidecar.

The default action is read-only and prints the sidecar health document. Root
registration and agent publication are opt-in. A live sidecar additionally
requires ``--confirm-live`` so an operator cannot accidentally send Sepolia
transactions while checking a local setup.

Examples::

    python scripts/setup_agent_names.py
    python scripts/setup_agent_names.py --setup-root --confirm-live
    python scripts/setup_agent_names.py --agent-label qa-test-planner \
      --agent-id AGT-Z48W-5F9X-8 --payout-address 0x... \
      --endpoint https://example.test/api/tasks --confirm-live

The operator key and sidecar token stay in the sidecar process environment;
this helper never accepts or prints private keys.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests


def _body(response: requests.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        value = {"error": response.text[:500]}
    return value if isinstance(value, dict) else {"value": value}


def _request(base_url: str, token: str, method: str, path: str,
             *, timeout: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method, f"{base_url.rstrip('/')}{path}",
        headers={"X-Sidecar-Token": token, "Accept": "application/json"},
        json=payload, timeout=timeout,
    )
    body = _body(response)
    if not response.ok:
        detail = body.get("error") or body.get("code") or f"HTTP {response.status_code}"
        raise RuntimeError(f"ENS sidecar {method} {path} failed: {detail}")
    return body


def _validate_agent_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    values = (args.agent_id, args.payout_address, args.endpoint)
    if args.agent_label and not all(values):
        parser.error("--agent-label requires --agent-id, --payout-address, and --endpoint")
    if any(values) and not args.agent_label:
        parser.error("agent fields require --agent-label")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("ENS_SIDECAR_URL")
                        or "http://127.0.0.1:8787")
    parser.add_argument("--token", default=os.environ.get("ENS_SIDECAR_TOKEN", ""),
                        help="sidecar token; defaults to ENS_SIDECAR_TOKEN")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--setup-root", action="store_true",
                        help="register or resume the configured root name")
    parser.add_argument("--duration-days", type=int, default=365)
    parser.add_argument("--agent-label", help="agent label below the configured root")
    parser.add_argument("--agent-id", help="public agent ID for --agent-label")
    parser.add_argument("--payout-address", help="Sepolia payout address")
    parser.add_argument("--endpoint", help="HTTPS agent endpoint recorded as mcp")
    parser.add_argument("--confirm-live", action="store_true",
                        help="allow writes when the sidecar reports live mode")
    args = parser.parse_args(argv)
    _validate_agent_args(args, parser)

    if not args.token:
        parser.error("ENS_SIDECAR_TOKEN is required")

    try:
        health = _request(args.base_url, args.token, "GET", "/health",
                          timeout=args.timeout)
        print(json.dumps({"health": health}, indent=2, sort_keys=True))
        writes_requested = bool(args.setup_root or args.agent_label)
        if not writes_requested:
            return 0
        if health.get("mode") != "dry_run" and not args.confirm_live:
            raise RuntimeError("live writes require --confirm-live")
        if not health.get("ok"):
            raise RuntimeError("sidecar health check is not ready; no write attempted")

        output: dict[str, Any] = {"health": health}
        if args.setup_root:
            output["root"] = _request(
                args.base_url, args.token, "POST", "/names/root/setup",
                timeout=max(args.timeout, 90.0),
                payload={"duration_days": args.duration_days},
            )
        if args.agent_label:
            output["agent"] = _request(
                args.base_url, args.token, "POST", "/names/agent", timeout=args.timeout,
                payload={
                    "label": args.agent_label,
                    "agent_public_id": args.agent_id,
                    "records": {"payout": args.payout_address, "mcp": args.endpoint},
                },
            )
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except (OSError, requests.RequestException, RuntimeError) as exc:
        print(f"setup_agent_names: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
