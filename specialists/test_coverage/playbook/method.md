# Method: baseline, targets, uplift

## M1 baseline checklist

1. Record the revision (`git rev-parse HEAD`), language/tool versions and
   the exact install and test commands under **Reproduce**.
2. Produce a coverage report with branch data where the tool supports it,
   measuring production code only (test modules never count toward
   acceptance) and writing data and report outside `repo/`:
   - Python: `coverage run --branch --source=<package> --data-file=<path under
     deliverables/> -m pytest`, then `coverage xml --data-file=<same> -o <report>`
   - JS/TS: a Cobertura or LCOV reporter from the existing runner
   - JVM: JaCoCo XML from the build plugin
   - Go: `go test -coverprofile=...` (statement-level; no branch data)
   Save it as `deliverables/m1-baseline/coverage-baseline.xml` (the name is
   fixed; the parser detects the format from content, so an LCOV or Go
   profile saved under that name is fine).
3. `parse_coverage` with `out: deliverables/m1-baseline/coverage-summary.json`.
4. `run_test_matrix` with `runs: 5`, `runs_dir: deliverables/m1-baseline/runs`
   and a command that writes JUnit XML to `{junit}` and varies order with
   `{seed}` when the stack supports it. Then `parse_test_results` with
   `out: deliverables/m1-baseline/flake-census.json`.
5. `git_churn` (default 180 days) to `deliverables/m1-baseline/churn.csv`,
   then `rank_targets` to `deliverables/m1-baseline/targets.csv`.
6. Write `baseline.md` with the sections the milestone lists. Explain every
   exclusion (generated code, vendored code, migrations) under **Out of scope**.

## Risk formula (documented so the customer can audit it)

`risk = (1 - line_pct/100) x log2(2 + commits) x log2(2 + lines)`

Files that are large, change often and are mostly untested rank first. A
fully covered file scores 0. Coverage tools name files differently from
git (JaCoCo by package, Go by import path, LCOV often absolutely), so each
report path takes the churn of the git path it shares the longest path
suffix with; `rank_targets` lists ambiguous matches, which the plan should
resolve. The proposed floor per file defaults to the current line coverage
+ 20pp, rounded up to 5, capped at 90; adjust it in the plan with a reason,
never silently.

## Default positions (unless the SOW says otherwise)

| Topic | Default |
|---|---|
| Coverage metric for acceptance | line; branch reported alongside |
| M3 gain | +20pp on the approved scope |
| Mutation floor on targets | 60% |
| Stability | 10 runs, varied order, all green |
| Patch size | <= 400 changed lines (M3), <= 800 (M2) |
| Runtime growth | <= 15% of suite wall time |
| Production changes | none |

## M3 uplift loop

For each target, in ranked order, until the scope reaches the target:

1. Read the missing lines from the coverage summary and the code around them.
2. Read the nearest existing tests; reuse their fixtures and style.
3. Write one small test for one behavior; run it alone, then with its file.
4. Re-measure coverage for the file; keep the test only if coverage rose or
   it kills a mutant that survived before.
5. Every few tests, re-run the mutation tool scoped to the target files and
   turn survivors into the next tests.

Measure `coverage-before.xml` at the start of M3, before adding a test (the
M1 report predates M2's tests, whose coverage M3 does not get to claim),
measure `coverage-after.xml` at the end with the same command and options,
and check the delta with `coverage_delta` before writing the report. Both
reports must measure the same production files: test modules are left out,
a file measured on one side only fails acceptance, and so does a falling
line total (excluding files or deleting code to lift a percentage).
