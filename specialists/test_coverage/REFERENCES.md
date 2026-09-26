# References (ideas only)

Clean-room: nothing below was copied (code, prompts, tool descriptions,
fixtures or data). We read public documentation and papers for ideas and
wrote our own implementation. Copyleft sources were used for ideas from
their public docs and papers only.

| Source | License | Idea we used |
|---|---|---|
| plasma-umass/coverup (and its FSE 2025 paper, arXiv 2403.16218) | Apache-2.0 | Treat uncovered regions as the work queue; keep a generated test only if it passes in isolation, repeatedly, and adds coverage; fetch context on demand rather than stuffing it into the prompt |
| qodo-ai/qodo-cover (formerly cover-agent) | AGPL-3.0 | Ideas from its public README only: the build / pass / add-coverage filter for keeping generated tests |
| Alshahwan et al., "Automated Unit Test Improvement using Large Language Models at Meta" (FSE 2024 industry track) | paper | Assured filters for LLM-written tests (builds, passes repeatedly, raises coverage) |
| Meta, "Mutation-Guided LLM-based Test Generation" (2025) | paper | Turn surviving mutants into targets for the next tests |
| se2p/pynguin | MIT | Keep only assertions that kill mutants; a search-based baseline beside the model |
| randoop/randoop | MIT | Regression oracles from observed values, and filtering non-deterministic observations before pinning them (characterization tests) |
| hcoles/pitest, stryker-mutator/stryker-js, boxed/mutmut, sourcefrog/cargo-mutants | Apache-2.0 / Apache-2.0 / BSD-3-Clause / MIT | Mutation score as the acceptance metric that defeats coverage gaming; scope and time-box mutation runs |
| stryker-mutator/mutation-testing-elements (report schema) | Apache-2.0 | The public JSON report shape (`files` -> `mutants` with a status) that `parse_mutation_report` reads; parser written from the schema description |
| Cobertura, JaCoCo, LCOV (geninfo) and Go cover profile formats | format docs | Report layouts parsed by `parse_coverage`; parsers written from the format descriptions |
| keploy/keploy | Apache-2.0 | Noted for a future integration-test milestone (record/replay with dependency fakes); not used in v1 |

Benchmarks considered for offline evaluation (not bundled; licenses vary,
TestGenEval is non-commercial and internal-only): TestGenEval, SWT-bench,
Defects4J, SWE-smith seeded faults.
