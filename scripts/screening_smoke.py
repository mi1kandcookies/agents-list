#!/usr/bin/env python3
"""
screening_smoke.py - manual check of the Intercepta screening integration
(not run in CI).

    python scripts/screening_smoke.py 0x<mainnet address>
    python scripts/screening_smoke.py 0x<mainnet address> --hop payee.onboard
    python scripts/screening_smoke.py 0x<mainnet address> --typed-data permit.json

With INTERCEPTA_API_KEY set (env or .env) it calls the live API, prints each
raw response and then the verdict. Without a key it sends nothing and prints
the fail-closed verdict. The address is screened as given (no Sepolia
mapping); the verdict is stored in a throwaway in-memory database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.screening.intercepta import InterceptaClient  # noqa: E402
from app.screening.service import HOPS, InterceptaScreener  # noqa: E402


class RecordingHttp:
    """requests wrapper that prints every request and raw response."""

    def __init__(self):
        import requests
        self._requests = requests

    def _show(self, method, url, resp):
        print(f"\n{method} {url} -> HTTP {resp.status_code}")
        try:
            print(json.dumps(resp.json(), indent=2))
        except ValueError:
            print(resp.text[:1000])
        return resp

    def get(self, url, **kwargs):
        return self._show("GET", url, self._requests.get(url, **kwargs))

    def post(self, url, **kwargs):
        return self._show("POST", url, self._requests.post(url, **kwargs))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual Intercepta screening smoke check.")
    parser.add_argument("address", help="mainnet address to screen")
    parser.add_argument("--hop", default="milestone.release", choices=sorted(HOPS))
    parser.add_argument("--amount-usdc", type=float, default=5.0)
    parser.add_argument("--typed-data", help="path to an EIP-712 JSON payload (Scan Message)")
    args = parser.parse_args()

    typed_data = None
    if args.typed_data:
        with open(args.typed_data, encoding="utf-8") as fh:
            typed_data = json.load(fh)

    app = create_app("testing")
    client = InterceptaClient(http=RecordingHttp())
    print(f"base url: {client.base_url}   key set: {client.configured}   timeout: {client.timeout:g}s")
    if not client.configured:
        print("INTERCEPTA_API_KEY not set: no request is sent; expect a fail-closed REFUSE.")
    addr = args.address.lower()
    screener = InterceptaScreener(client, address_map={addr: addr})
    with app.app_context():
        db.create_all()
        verdict = screener.screen(args.hop, chain_address=addr,
                                  amount_micro=round(args.amount_usdc * 1_000_000),
                                  typed_data=typed_data)
    print("\nverdict:")
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
