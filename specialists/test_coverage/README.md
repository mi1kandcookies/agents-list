# Test Coverage & Characterization Engineer (`test-coverage`)

A first-party specialist that makes one customer repository safer to change.
It measures coverage and flakiness, pins current behavior before a refactor,
and raises coverage with tests that must kill mutants. It changes **test
code only**. The harness builds the patch and, for M2 and M3, re-runs the
test suite itself; the acceptance checks recount every coverage and
mutation figure from the raw reports (which the agent's own tool runs
produce, see Known limits).

## Milestones

| id | What the customer gets | Automated acceptance (recomputed) |
|---|---|---|
| `m1-baseline` | `baseline.md`, raw coverage report, per-file summary, five-run flake census, churn, risk-ranked `targets.csv` with proposed floors | `coverage_summary_matches`, `flake_census_matches`, `targets_ranking_matches`, plus kit `files_exist`, `markdown_sections`, `no_placeholders`, `csv_columns` |
| `m2-characterization` | Characterization tests for the approved targets as `repo.patch`, `suspicious-behaviors.md`, ten-run evidence, mutation report | `diff_test_paths_only`, `patch_size_max` (800), `no_assertion_free_tests`, `patch_secret_free`, `tests_stable` (10 harness runs; every added test passes in each, M1's tests still run), `mutation_score_min` (60%) |
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

`CoverageSpecialist` adds two run hooks to the kit's defaults, both built on
`snapshot.RepoStore`, a private git object store under
`.agentkit/test-coverage/` that reads `repo/` only as a work tree:

- `prepare` snapshots `repo/` the first time any milestone starts (the
  origin), and fixes each patch milestone's base the first time it starts
  (kept across resumed or repeated runs): the tree the last submitted patch
  milestone delivered, else the origin. So M3's patch never repeats M2's
  tests, and production edits made in any earlier milestone surface in the
  next patch. A `patch_base` event lists any drift of `repo/` from the base.
- `finalize` rebuilds each `repo.patch` as the diff from that base to
  `repo/` now (new and binary files included) and overwrites what the model
  wrote. The patch checks therefore grader the real change: committing an
  edit, skip-worktree bits, diff-prefix or exclude settings in `repo/.git`,
  or moving `repo/.git` away change nothing, because every git command runs
  on the private store with an isolated configuration. If the patch cannot
  be rebuilt (an embedded repository, `repo/` replaced by a link) it is
  removed and the patch checks fail closed. A `patch_rebuilt` event records
  the base, the delivered tree, whether the model's patch matched, and any
  paths outside the test globs.

`repo/` need not be a git checkout (an uploaded archive works as is). New
files covered by `repo/`'s `.gitignore`, and common tool output (coverage
data, caches, virtualenvs, `*.egg-info/`), are left out of the patch; files
the base tracks are always compared. The `export_patch` tool previews the
same patch from the same store.

After the patch, `finalize` re-runs the suite for each `tests_stable`
criterion: the command the model last gave `run_test_matrix` (recorded in
`runs/matrix.json`), at least `min_runs` times, against `repo/` as
delivered. Its run files replace the model's; if there is no usable command
(none recorded, inline code, outside `repo/`, milestone not submitted) the
run files are removed and `tests_stable` fails closed. `tests_stable` then
requires every test the rebuilt patch adds to pass in every run, every run
to finish (`matrix.json` log), and, through `baseline`, every test an
earlier milestone's runs showed to still run; tests those runs showed as
flaky or broken are reported, not counted, since the customer's existing
flakes are out of scope. The test ids each submitted milestone's runs
showed are kept in `.agentkit/test-coverage/state.json`.

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
- The patch and the M2/M3 test runs are the harness's, but the command it
  re-runs is the one the model recorded (inline `python -c` / `node -e` is
  refused; a runner script the patch adds is visible in the patch). M1's
  runs, every coverage report and the mutation report come from the
  agent's own tool runs: the checks recount them from the raw files but
  cannot tell a tool's output from a hand-edited file, and M3's
  `coverage-before.xml` is not tied to the milestone's base. Re-measuring
  coverage and mutants on the platform's side would close that gap.
- New files that a `.gitignore` added during the engagement covers are left
  out of the patch like any ignored file, so the customer would not receive
  them (production files the base tracks are always compared).
- An allowlisted interpreter or git can also touch files outside the tools'
  rules, so this specialist must run in an OS sandbox (see "Containment" in
  docs/decisions/0002-specialist-kit.md). The harness's own git commands
  run with the kit's privileges on the private store and read `repo/` as a
  plain work tree with no model-controlled configuration.
