"""
agentkit - the shared, provider-neutral kit that every first-party specialist
agent on Agent's List runs on.

A specialist is a manifest (agent.yaml), prompts, domain tools and acceptance
checks; the kit supplies the model adapters, the tool loop, sandboxed tools,
the claim ledger, checks, evidence hashing and the harness CLI
(`python -m agentkit`). Flask-free, like chain/. See
docs/decisions/0002-specialist-kit.md.
"""
from __future__ import annotations

__all__: list[str] = []
