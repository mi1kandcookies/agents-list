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
- Scans every text file for hard-coded secrets (cloud, GitHub, Stripe and
  Google keys, private keys, tokens, credential assignments in code and
  config, credentials in connection URLs), with redacted evidence and a list
  of what it skipped.
- Audits dependencies against the OSV vulnerability database, picking the fix
  on the installed version's branch and listing what it could not audit.
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
- `osv_lookup` (optional, default yes) - whether the dependency audit may send
  pinned package names and versions (nothing else) to the public OSV database
  at `api.osv.dev`. `false` denies the audit tool.
- `internal_packages` (optional) - names or prefixes (`acme-*`) never sent to
  OSV; npm packages resolved from a private registry are never sent either.

Run it with the agentkit CLI, e.g. `python -m agentkit milestones appsec-review`
or `python -m agentkit validate-intake appsec-review --intake intake.json`
(`authorized` and `repo_provided` must be `true`; a run refuses to start
without `authorized: true` in the brief's intake).

## What the checks recompute

The acceptance checks never take the agent's word for it. They re-run the
secret scanner (every hit must be reported or dismissed with a reason, every
`SECRET.*` result must match a hit, and no secret value may appear in a
deliverable), match every other SARIF location against a source snippet,
re-score every CVSS vector against the level and `security-severity` it
carries, look up every CVE/GHSA/PYSEC/OSV id in the OSV answers the kit
recorded, re-parse the API spec against the attack-surface map, and check
every host the deliverables name against the signed scope (out-of-scope
entries win; an exact host does not cover its subdomains). An empty findings
register passes when it says so plainly: the agent is never pushed to invent
a finding.

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
  Only pinned package names and versions are sent there, and only with the
  customer's consent (`osv_lookup`).
- Run limits (steps/tokens/USD/wall-clock) are in `agent.yaml`.
- A clean result is not proof of security; this is not a substitute for a full
  manual penetration test.

## Layout

```
agent.yaml            manifest (milestones, tools, checks, human gate, limits)
agent.py              Specialist subclass (authorization gate, OSV consent)
prompts/system.md     persona and method
playbook/*.md         review method, OWASP checklist, CVSS guidance, defaults
rubrics/*.yaml        grader rubrics (threat model, report)
tools.py              domain tools + TOOL_DEFS
checks.py             acceptance checks + CHECK_DEFS
evals/fixtures/       synthetic inputs (fictional Northwind Ledger, including a
                      prompt-injection comment the review must ignore)
evals/cases/*.json    golden cases, one per milestone
```
