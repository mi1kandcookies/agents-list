# Application Security Review specialist

White-box, static-first application security reviewer. It reads a customer's
source under a signed scope, models the threat surface, finds real weaknesses,
and produces a SARIF 2.1.0 report with file:line evidence, a CVSS score, an
exploit scenario and a code-level fix per finding. It never runs live exploits
and never touches anything outside the signed allow-list.

## What it does

- Confirms the signed scope and refuses out-of-scope targets.
- Builds a threat model and attack-surface map from an OpenAPI/Swagger spec and
  the source.
- Scans for hard-coded secrets (redacted evidence).
- Audits dependencies against the OSV vulnerability database.
- Traces user input to dangerous sinks and scores findings with CVSS v3.1.
- Emits SARIF 2.1.0 that imports into GitHub code scanning, plus a human-readable
  report.

## Milestones

1. **Scope, threat model and attack-surface map** - scope confirmation, endpoint
   inventory, trust boundaries, test plan.
2. **Validated findings with SARIF evidence** - secrets, vulnerable dependencies
   and source->sink findings, each evidenced, classed, scored and fixed.
3. **Remediation guidance and final report** - per-finding fixes and an
   auditor-ready report with an explicit coverage-limits disclaimer.

## Inputs (intake)

- `repo_provided` (required) - read-only source access.
- `authorized` (required) - customer confirms ownership/authorization.
- `scope_file` (required) - the signed allow-list at `inputs/scope.json`.
- `openapi_spec` (optional) - path to an API spec for better coverage.
- `environment` (optional) - staging by default; production needs written approval.
- `owasp_categories` (optional) - categories to prioritise.

## Human gate

Human-gated (reviewer role: **customer security lead**). Findings and the final
report are draft work product: a qualified human triages every finding and
approves the report before it is used for compliance or attestation. The agent
never signs off and never performs active exploitation.

## Limits and safety

- Static/read-only by default; no live exploitation, no destructive testing, no
  data exfiltration.
- Scope is enforced: `check_scope` before any target; production out of scope
  without written approval.
- Network egress is allow-listed to `api.osv.dev` only; shell is disabled.
- Run limits (steps/tokens/USD/wall-clock) are in `agent.yaml`.
- A clean result is not proof of security; this is not a substitute for a full
  manual penetration test.

## Layout

```
agent.yaml            manifest (milestones, tools, checks, human gate, limits)
prompts/system.md     persona and method
playbook/*.md         review method, OWASP checklist, CVSS guidance, defaults
rubrics/*.yaml        grader rubrics (threat model, report)
tools.py              domain tools + TOOL_DEFS
checks.py             acceptance checks + CHECK_DEFS
evals/fixtures/       synthetic inputs (fictional Northwind Ledger)
evals/cases/*.json    golden cases, one per milestone
```
