"""
specialists/appsec_review - application security review specialist.

A white-box, static-first AppSec reviewer: it reads a customer's source under
a signed scope, builds a threat model and attack-surface map from API specs,
scans for hard-coded secrets, audits dependencies against OSV, and emits
SARIF 2.1.0 findings with file:line evidence, a CVSS score, an exploit
scenario and a fix per finding. It never performs active/live exploitation and
never tests outside the signed allow-list; a customer security lead triages the
findings before the report is used for compliance or attestation.

This package ships the domain pack only (manifest, prompts, playbook, rubrics,
domain tools and acceptance checks). The agentkit Specialist wrapper (agent.py)
is added when the kit lands; nothing here imports from agentkit beyond
agentkit.types / agentkit.errors.
"""
