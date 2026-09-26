#!/usr/bin/env python3
"""
world_smoke.py - manual check of the World ID OIDC integration (not run in CI).

    python scripts/world_smoke.py            # print the live discovery document
    WORLD_CLIENT_ID=... WORLD_CLIENT_SECRET=... python scripts/world_smoke.py

With client credentials set it also starts a device authorization (sending a
random nonce unless --no-nonce), prints the user code and verification URL,
polls until the human approves or the code expires, validates the ID token and
prints the claims. Reads WORLD_ISSUER (default: the sandbox).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import jwt  # noqa: E402

from app.identity.pkce import b64url  # noqa: E402
from app.identity.world import IdTokenError, WorldClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual World ID OIDC smoke check.")
    parser.add_argument("--no-nonce", action="store_true",
                        help="start the device flow without a nonce")
    parser.add_argument("--max-age", type=int, default=600,
                        help="accepted auth_time age in seconds (default 600)")
    args = parser.parse_args()

    client = WorldClient()
    print(f"issuer: {client.issuer}")
    print(json.dumps(client.discovery(), indent=2))
    print(f"jwks kids: {[k.get('kid') for k in client.jwks().get('keys', [])]}")

    if not client.configured:
        print("\nWORLD_CLIENT_ID / WORLD_CLIENT_SECRET not set; skipping the device flow.")
        return 0

    nonce = None if args.no_nonce else b64url(os.urandom(32))
    started = int(time.time())
    start = client.device_authorize(nonce=nonce)
    print(f"\nuser code:  {start.user_code}")
    print(f"open:       {start.verification_uri_complete or start.verification_uri}")
    print(f"expires in: {start.expires_in}s, nonce sent: {nonce is not None}")

    interval = start.interval
    deadline = time.time() + start.expires_in
    while time.time() < deadline:
        time.sleep(interval)
        poll = client.poll_device(start.device_code)
        interval = poll.interval or interval
        print(f"poll: {poll.status}" + (f" ({poll.error})" if poll.error else ""))
        if poll.status in ("pending", "slow_down"):
            continue
        if poll.status != "approved":
            return 1
        # Report whether the provider echoes the nonce in device-flow tokens.
        echoed = jwt.decode(poll.id_token, options={"verify_signature": False}).get("nonce")
        print(f"nonce echoed in id_token: {echoed is not None}")
        try:
            claims = client.validate_id_token(
                poll.id_token, expected_nonce=nonce if echoed is not None else None,
                not_before=started, max_age_s=args.max_age)
        except IdTokenError as exc:
            print(f"id_token rejected: {exc}")
            return 1
        print(json.dumps(asdict(claims), indent=2))
        return 0
    print("device code expired")
    return 1


if __name__ == "__main__":
    sys.exit(main())
