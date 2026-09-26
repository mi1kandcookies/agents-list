# Test Coverage & Characterization Engineer (`test-coverage`)

A first-party specialist that makes one customer repository safer to change.
It measures coverage and flakiness, pins current behavior before a refactor,
and raises coverage with tests that must kill mutants. It changes **test
code only**, and every number it reports is recomputed by the acceptance
checks from raw coverage, JUnit and mutation reports.

## Milestones

| id | What the customer gets | Automated acceptance (recomputed) |
|---|---|---|
| `m1-baseline` | `baseline.md`, raw coverage report, per-file summary, five-run flake census, churn, risk-ranked `targets.csv` with proposed floors | `coverage_summary_matches`, `flake_census_matches`, `targets_ranking_matches`, plus kit `files_exist`, `markdown_sections`, `no_placeholders`, `csv_columns` |
| `m2-characterization` | Characterization tests for the approved targets as `repo.patch`, `suspicious-behaviors.md`, ten-run evidence, mutation report | `diff_test_paths_only`, `patch_size_max` (800), `no_assertion_free_tests`, `patch_secret_free`, `tests_stable` (10 runs), `mutation_score_min` (60%) |
| `m3-coverage-uplift` | Unit tests as `repo.patch`, before/after coverage, ten-run evidence, mutation report, `uplift-report.md` | `coverage_delta_min` (+20pp line, measured code must not shrink), `diff_test_paths_only`, `patch_size_max` (400), `no_assertion_free_tests`, `patch_secret_free`, `tests_stable`, `mutation_score_min` |

Each milestone also has a `rubric_grader` check (rubrics in `rubrics/`) and a
`human_signoff` for the customer's decision: approve targets (M1), triage
suspicious behaviors (M2), review and merge (M3). Thresholds in `agent.yaml`
are defaults; the signed SOW's milestone acceptance can tighten or scope them
(for example `scope: [billing/]` on `coverage_delta_min`).

## Inputs (intake)

Required: `repo` (URL or archive plus revision), `test_command` (and needed
services), `language_stack`. Optional: `target_modules`,
`coverage_target_pp`, `mutation_floor_pct`, `runtime_budget_pct`,
`production_change_policy`.

The repository is checked out to `repo/`; customer notes go in `inputs/`
(read-only); deliverables land in `deliverables/<milestone-id>/`.

## Tools

Kit tools: `read_document`, `read_file`, `write_file`, `edit_file`,
`list_files`, `search_files`, `run_command`, `ask_client`, `post_progress`,
`submit_milestone`.

Domain tools (`tools.py`, deterministic):

| tool | does |
|---|---|
| `parse_coverage` | Cobertura / JaCoCo / LCOV / Go profile -> per-file line and branch counts, missing lines |
| `coverage_delta` | before/after deltas over a scope, with measured line totals |
| `run_test_matrix` | runs the suite N times via the kit runner, JUnit per run, flake census |
| `parse_test_results` | census from existing JUnit files |
| `diff_scope` / `export_patch` | patch files, sizes and anything outside test paths |
| `find_assertion_free_tests` | tests with no real assertion (Python AST; JS/TS, Java/Kotlin, Go heuristics) |
| `parse_mutation_report` | mutation score and surviving mutants (mutation-testing-elements JSON) |
| `git_churn` / `rank_targets` | churn x size x uncovered-share risk ranking |
| `scan_patch_secrets` | secret-looking values in added lines (reports location, never the value) |

Domain tools resolve every path through the kit's policy gate
(`resolve_path`): no escape from the workspace, `inputs/` read-only,
`.agentkit/` off limits. git writes patches and churn logs straight to disk,
so the kit's cap on command output never truncates them.

## The harness owns the patch (`agent.py`)

`CoverageSpecialist` adds two run hooks to the kit's defaults:

- `prepare` records the commit `repo/` is at when a milestone that delivers
  a `repo.patch` starts (kept under `.agentkit/test-coverage/`, and kept
  across a resumed run).
- `finalize` rebuilds each `repo.patch` from `git diff <that commit>` over
  `repo/`, new files included, and overwrites what the model wrote. The
  patch checks therefore grader the real change: a production edit that the
  model committed, or left out of a hand-written patch, still fails
  `diff_test_paths_only`. If the patch cannot be rebuilt (for example
  `repo/.git` was moved away) it is removed and the patch checks fail
  closed. A `patch_rebuilt` event records the commit, whether the model's
  patch matched, and any paths outside the test globs.

This needs `repo/` to be a git checkout with a commit; for an uploaded
archive without history the platform should commit it once before M2, or
the patch is checked as the model wrote it (a `patch_base` event says so).

## Human gate

No licensed reviewer is needed (`human_gate.required: false`), but the
customer decides at every milestone: targets and thresholds before M2, the
verdict on each suspicious behavior, and every merge. Production-code
changes are out of scope unless the customer approves a specific one in
writing. A pinned behavior is not a claim that the behavior is correct.

## Limits and safety

- `max_steps 150`, `max_tokens 6M`, `max_usd 60`, `max_wall_minutes 360`.
- Shell allowlist: common test toolchains (`git`, `python`, `pytest`,
  `coverage`, `mutmut`, `node`/`npm`/`npx`/`pnpm`/`yarn`, `go`, `mvn`,
  `gradle`, `dotnet`, `cargo`, `pip`, `uv`); 30-minute command timeout.
- Egress allowlist: package registries only. The customer's tests must run
  without network access; tests that need it are reported, not "fixed".
- The customer's code runs on the engagement VM; no production credentials
  belong there. Repository content is treated as data, never instructions.
- Coverage and JUnit XML with entity declarations is refused.

## Known limits (v1)

- The assertion check parses Python; other languages use block heuristics
  and may miss assertions made through custom helpers (pass `helpers`).
- Mutation reports must be in the mutation-testing-elements JSON shape;
  tools that emit other formats need a conversion step.
- Go profiles are statement-based, so "line" numbers for Go are statements
  and there is no branch metric.
- Integration tests (containers, recorded traffic) and a CI coverage ratchet
  are not milestones yet.
- The checks recompute every number from the JUnit, coverage and mutation
  files in the workspace, but they cannot tell a file a tool produced from
  one the model edited by hand; only the patch is rebuilt by the harness.
  Re-running the suite and coverage on the platform's side would close
  that gap. An allowlisted interpreter or git can also touch files outside
  the tools' rules, so this specialist must run in an OS sandbox (see
  "Containment" in docs/decisions/0002-specialist-kit.md).
