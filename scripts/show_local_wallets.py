#!/usr/bin/env python3
"""Print public addresses for the local Keychain-backed test wallets."""
from __future__ import annotations

import subprocess

from eth_account import Account


ROLES = (
    ("ENS operator", "ens-operator"),
    ("x402 facilitator", "facilitator"),
    ("escrow", "escrow"),
    ("buyer vault", "buyer-vault"),
    ("x402 payer", "x402-payer"),
)


def keychain_get(role: str) -> str:
    return subprocess.check_output(
        ["/usr/bin/security", "find-generic-password", "-a", "agents-list",
         "-s", f"agents-list.{role}", "-w"],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def main() -> int:
    for label, role in ROLES:
        try:
            address = Account.from_key(keychain_get(role)).address
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            raise SystemExit(f"{role}: unavailable ({type(exc).__name__})") from None
        print(f"{label}: {address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
