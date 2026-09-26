# Review method

The engagement is three milestones. Each hands the next a concrete artifact.

## M1 - Scope, threat model and attack-surface map

Goal: agree exactly what is in scope and describe what an attacker could reach.

1. Read `inputs/scope.json`. Verify `authorized` is true and the host allow-list
   is present. If not, `ask_client` and stop.
2. If an OpenAPI/Swagger spec exists, run `parse_openapi` to enumerate every
   operation, its method, its parameters and whether it requires auth.
3. Read the source to fill gaps the spec misses (undocumented routes, admin
   endpoints, background jobs, file uploads, deserialization points).
4. Identify assets (data stores, secrets, PII), trust boundaries (client ->
   API, API -> DB, API -> third parties) and the entry points that cross them.
5. Write `threat-model.md` with sections: **Scope**, **Assets**,
   **Trust Boundaries**, **Attack Surface**, **Test Plan**. Write
   `attack-surface.json` (the endpoint inventory + unauthenticated routes) - the
   `parse_openapi` output is a good base.

Deliverable acceptance: files exist, JSON is valid, the markdown has all five
sections, the rubric passes, and the customer confirms scope (human gate).

## M2 - Validated findings with SARIF evidence

Goal: a register of real, evidenced weaknesses.

1. `scan_secrets` over `repo/`. Write the raw hits to
   `deliverables/m2-findings/secrets.json` (list of {file, line, rule_id, ...}) -
   the reconcile check re-runs the scanner against these.
2. `audit_dependencies` against the manifest (requirements.txt or
   package-lock.json) via OSV. Register OSV advisories as sources.
3. Trace user input to dangerous sinks (SQL, shell, template, path, redirect,
   deserialization) and to broken access-control checks. Every finding needs a
   file:line, a source->sink narrative, and a fix.
4. Score each with `cvss_base_score` and tag CWE + OWASP category.
5. Assemble the finding objects and call `build_sarif` to write
   `findings.sarif`. Write a human-readable `findings.md` too.

Deliverable acceptance: SARIF is valid, every location resolves to a real
file:line with a matching snippet, every finding has class + exploit +
remediation, nothing is out of scope, claimed secrets reconcile, and no
placeholders remain. Customer triages severity (human gate).

## M3 - Remediation guidance and final report

Goal: something a developer can fix from and an auditor can read.

1. Write `remediation.md`: per-finding fix mapped to the exact file/line or
   config, ordered by severity, with retest steps.
2. Write `report.md` with sections: **Executive Summary**, **Scope**,
   **Methodology**, **Findings**, **Remediation**, **Coverage Limits**. Include
   the disclaimer verbatim (see default positions).

Deliverable acceptance: report exists with all sections, no placeholders, the
disclaimer is present, the rubric passes, and the customer security lead approves
(human gate).
