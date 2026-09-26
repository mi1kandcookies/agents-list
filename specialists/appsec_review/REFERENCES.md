# References

Clean-room notice: everything in this package was written from first principles.
No third-party code, prompt text, tool description, rule set or fixture was
copied. The sources below informed *ideas* only (architecture, method, output
format). Licenses are recorded so a future contributor knows what may and may not
be reused.

## Standards and specifications

- **SARIF 2.1.0** (OASIS Static Analysis Results Interchange Format) - the
  findings output format, so results import into GitHub code scanning.
  Idea used: the log/run/tool.driver/result/physicalLocation shape and the
  `security-severity` property. Public OASIS standard.
- **CVSS v3.1** (FIRST.org Common Vulnerability Scoring System) - the base-score
  formula and vector grammar re-implemented in `cvss_base_score`. FIRST publishes
  the specification and equations openly.
- **CWE** (MITRE Common Weakness Enumeration) and **OWASP Top 10** - taxonomies
  used to class findings. Public references.
- **GitHub code scanning SARIF support** (public documentation) - idea: code
  scanning reads `security-severity` from the rule's properties, only for
  rules tagged `security`, and CWE tags in the `external/cwe/cwe-N` form.
- **Token formats** published by the issuing vendors (AWS access key ids,
  GitHub `ghp_`/`github_pat_` tokens, Stripe `sk_live_` keys, Google `AIza`
  API keys, Slack `xox*` tokens) and the PEM private-key header - the secret
  patterns are written from these public format descriptions, not from any
  scanner's rule set.
- **PEP 440** and **Semantic Versioning** - the version ordering used to
  place an installed version inside an OSV affected range.
- **OSV** (osv.dev, Open Source Vulnerabilities) - queried via the public
  `api.osv.dev` query API for the dependency audit. Idea used: query-by-package
  request shape and the affected-ranges/fixed-version structure. Data is
  CC-BY-4.0; the service is queried at runtime, nothing is vendored.

## OSS projects reviewed for ideas (not copied)

- **usestrix/strix** (Apache-2.0) - idea: a findings "closure discipline"
  (confirmed / ruled-out / open) to cut false positives, and a SARIF + CVSS +
  dedupe reporting pipeline as a milestone deliverable. Code not used.
- **KeygraphHQ/shannon** (AGPL-3.0, copyleft - ideas from documentation only) -
  idea: the "no exploit / no complete trace, no finding" acceptance bar and an
  explicit rules-of-engagement / scope configuration. No code or text reused.
- **anthropics/claude-code-security-review** (MIT) - idea: a two-stage
  generate-then-filter approach to false positives and treating prompt templates
  as reviewable assets; and the key lesson that source under review is untrusted
  input (prompt-injection risk). Code not used.
- **vxcontrol/pentagi** (MIT) - idea: a supervisor/critic loop that detects
  stalls and forces a strategy change on long engagements. Code not used.
- **GreyDGL/PentestGPT** (MIT) - idea: a staged pipeline with explicit hand-off
  artifacts between phases, matching our milestone decomposition. Published at
  USENIX Security 2024 (citable methodology). Code not used.

All fixtures under `evals/fixtures/` are synthetic and describe a fictional
company (Northwind Ledger); any secret-shaped strings used in tests are assembled
at runtime, never committed.
