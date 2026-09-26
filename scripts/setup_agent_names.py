#!/usr/bin/env python3
"""Check the live ENSv2 records used by a named specialist.

Namespace registration and resolver writes are intentionally operator-owned.
This script is the reproducible read/check half: it prints the records and the
least-privilege Permissioned Resolver calls an owner should perform.
"""
from __future__ import annotations

import argparse
import json

from chain.ens_v2 import resolve_agent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="ENSv2 specialist name")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    resolved = resolve_agent(args.name)
    if args.as_json:
        print(json.dumps(resolved.snapshot(), indent=2, sort_keys=True))
        return 0
    print(f"name:     {resolved.name}")
    print(f"resolver: {resolved.resolver}")
    print(f"address:  {resolved.address}")
    print(f"endpoint: {resolved.endpoint}")
    print(f"enabled:  {resolved.enabled}")
    print(f"status:   {resolved.status}")
    print(f"source:   {resolved.source}")
    print("\nPermissioned Resolver demonstration calls:")
    print("  grantSetterRoles(encode(setText(0x00, 'com.agentslist.status', '')), specialistWriter)")
    print("  setText(dnsName, 'com.agentslist.status', 'ready')  # must succeed")
    print("  setText(dnsName, 'com.agentslist.work-endpoint', '...') # must revert")
    print("  revokeRoles(keccak256('com.agentslist.status'), ROLE_SET_TEXT, specialistWriter)")
    print("  setText(dnsName, 'com.agentslist.status', 'offline') # must now revert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
