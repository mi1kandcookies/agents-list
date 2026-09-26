# References

Ideas only; no code, prompts, tool descriptions, fixtures or data were
copied. Copyleft projects were consulted through their public docs only.

| source | license | idea used |
|---|---|---|
| google/osv-scanner | Apache-2.0 | Scan pinned versions against OSV; rank candidate upgrades by advisories fixed per change; present in-place / relock / override as risk tiers |
| OSV schema and API (osv.dev) | Apache-2.0 (schema), CC-BY-4.0 (data) | `querybatch` + per-id records; evaluate `introduced` / `fixed` / `last_affected` events locally |
| FIRST CVSS v3.1 specification | public standard | Base-score formula and severity bands, implemented from the spec |
| dependabot/dependabot-core | MIT | Per-ecosystem parse / resolve / update split; pull changelogs into the context for breaking changes (interface ideas only) |
| renovatebot/renovate | AGPL-3.0 (docs only) | Minimum release age as a supply-chain guard; grouping coupled packages |
| anthropics/code-migration-kit-with-claude-code | Apache-2.0 | Build or validate the grader before changing code; settle translation decisions once as rules; recurring failures amend the rules, with human approval |
| konveyor/kai | Apache-2.0 | Validators feed a task queue; re-validate after each fix; resolved-incident memory |
| ast-grep/ast-grep | MIT | An old-API detector whose match count must reach zero as an objective milestone metric |
| openrewrite/rewrite (core) | Apache-2.0 | Deterministic transforms first, model only for the long tail; dry-run patches as evidence |
| MigrationBench (arXiv 2505.09569) | Apache-2.0 | Acceptance criteria: build and tests pass, test count non-decreasing, test methods unchanged, target actually in effect |
| trailofbits/buttercup | AGPL-3.0 (docs only) | Patch validation discipline: re-run the full suite, reject on any regression |
| OpenVEX / CycloneDX VEX | Apache-2.0 | Not-affected claims need a justification and a named human approver (residual-risk register statuses) |
