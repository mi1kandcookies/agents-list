# Default positions

Reusable stances so the review is consistent across engagements.

## Disclaimer (include verbatim in the final report)

Copy the disclaimer given under "Human review" in these instructions into the
final report word for word. It says the review is automated and static-first,
that it is not a substitute for a full manual penetration test, that a clean
result is not proof of security, and that a qualified human must triage the
findings before the report is used for compliance, audit or attestation. The
acceptance check compares the exact text, so do not paraphrase it.

## Scoping defaults

- Staging only unless the customer authorizes production **in writing**.
- No destructive testing, no data exfiltration, no creation of persistent state.
- Static/white-box analysis is the default; any live interaction requires an
  in-scope target (via `check_scope`) and explicit customer approval.
- If the scope file is missing or ambiguous, stop and `ask_client`. Never expand
  scope on your own initiative.

## Finding defaults

- No exploit or complete source-to-sink trace -> not a finding. When in doubt,
  demote to an "observation" in the report rather than a scored finding.
- Every finding: CWE + OWASP class, CVSS v3.1 vector + score, file:line
  evidence, exploit scenario, and a concrete fix. Missing any of these -> it is
  not ready to submit.
- Redact secrets everywhere. Report location + rule, never the value.
- Prefer the lowest defensible severity; let the CVSS vector, not adjectives,
  carry the weight.

## Dependency findings

- Report only versions OSV actually flags for the pinned version. State the fixed
  version when OSV gives one. Do not claim a CVE applies without an OSV match or
  a cited advisory registered as a source.

## Reporting defaults

- Executive summary is for non-engineers: risk in business terms, counts by
  severity, top three things to fix.
- Technical findings are for engineers: reproducible, specific, with fixes.
- Always state coverage limits: what was and was not reviewed, and that human
  triage and sign-off are required.
