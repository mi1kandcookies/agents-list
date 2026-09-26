# Review method

The engagement is three milestones. Each hands the next a concrete artifact.

## M1 - Scope, threat model and attack-surface map

Goal: agree exactly what is in scope and describe what an attacker could reach.

1. Authorization is already confirmed by the intake (a run does not start
   without `authorized: true`). Read `inputs/scope.json` and check that its
   `hosts` allow-list is present and non-empty. If it is not, `ask_client`,
   then continue with the source-only parts of the milestone and state the
   assumption.
2. If an OpenAPI/Swagger spec exists (JSON or YAML), run `parse_openapi` to
   enumerate every operation, its method, its parameters and whether it
   requires auth. An operation whose security list contains `{}` allows
   anonymous access and counts as unauthenticated.
3. Read the source to fill gaps the spec misses (undocumented routes, admin
   endpoints, background jobs, file uploads, deserialization points).
4. Identify assets (data stores, secrets, PII), trust boundaries (client ->
   API, API -> DB, API -> third parties) and the entry points that cross them.
5. Write `threat-model.md` with sections: **Scope**, **Assets**,
   **Trust Boundaries**, **Attack Surface**, **Test Plan**. Write
   `attack-surface.json` (the endpoint inventory + unauthenticated routes) - the
   `parse_openapi` output is the base; add what the source shows. Name
   out-of-scope hosts only under the **Scope** heading, to restate the scope.

Deliverable acceptance: files exist, JSON is valid, the attack-surface map
lists every operation of the spec and flags every unauthenticated one, no
out-of-scope host is a target, no secret value appears, the markdown has all
five sections, the rubric passes, and the customer confirms scope (human
gate).

## M2 - Validated findings with SARIF evidence

Goal: a register of real, evidenced weaknesses.

1. `scan_secrets` over `repo/`. Write `deliverables/m2-findings/secrets.json`
   as `{"findings": [...], "dismissed": [...]}`: every hit is either a
   finding ({file, line, rule_id, ...} as the scanner reported it) or
   dismissed with a `reason` (a test fixture, a documented example value).
   The reconcile check re-runs the scanner and fails on a hit that is in
   neither list; the human reviewer sees every dismissal.
2. `audit_dependencies` against the manifest (requirements.txt or
   package-lock.json) via OSV, unless the intake opted out. Each OSV answer
   that lists a vulnerability is registered as a source; its id is the
   `source` of each vulnerability. `fixed` is the fix on the installed
   version's branch; `unaudited` lists what could not be checked (ranges,
   includes, private packages) - carry it into the coverage limits.
3. Trace user input to dangerous sinks (SQL, shell, template, path, redirect,
   deserialization) and to broken access-control checks. Every finding needs a
   file:line, a source->sink narrative, and a fix.
4. Give each a CVSS v3.1 vector (check it with `cvss_base_score`) and tag
   CWE + OWASP category. `build_sarif` derives the score, band and level from
   the vector and refuses a score or severity that disagrees with it.
5. Assemble the finding objects and call `build_sarif` to write
   `findings.sarif` (finding files are relative to `repo/`; a `repo/...` path
   from `scan_secrets` is accepted). A code finding needs a snippet of at
   least 10 non-space characters copied from the line(s) it points at; a
   `SECRET.*` finding needs none (its location is checked against the scanner).
   Write a human-readable `findings.md` too; if nothing meets the bar, leave
   the SARIF log empty and say "No findings" there.

Deliverable acceptance: SARIF is valid, every location resolves to real
customer source (snippet or fresh scan), every finding has class + CVSS
vector + exploit + remediation and the score its vector gives, secrets
reconcile both ways and no secret value appears in a deliverable, every
advisory id is in a recorded OSV answer, no out-of-scope target, citations
resolve, and no placeholders remain. Customer triages severity (human gate).

## M3 - Remediation guidance and final report

Goal: something a developer can fix from and an auditor can read.

1. Write `remediation.md`: per-finding fix mapped to the exact file/line or
   config, ordered by severity, with retest steps.
2. Write `report.md` with sections: **Executive Summary**, **Scope**,
   **Methodology**, **Findings**, **Remediation**, **Coverage Limits**. Include
   the disclaimer given under "Human review" verbatim.

Deliverable acceptance: both files exist, the report has all sections, no
placeholders, no out-of-scope target, no secret value, every advisory id and
citation resolves, the disclaimer is present, the rubric passes, and the
customer security lead approves (human gate).
