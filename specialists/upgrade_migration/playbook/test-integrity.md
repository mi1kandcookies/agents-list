# Test integrity

The customer's test suite is the grader of every step. A green suite obtained
by weakening the grader is worthless and fails acceptance.

## Never

- delete a test file or a test function;
- add skip / xfail / disabled markers (`pytest.mark.skip`, `xfail`,
  `unittest.skip`, `it.skip`, `xit`, `describe.skip`, `t.Skip`, ...);
- loosen an assertion (exact -> approximate, equality -> truthiness, a
  specific exception -> a broad one) to absorb a behavior change;
- change CI configuration, test selection flags, coverage thresholds or the
  test command;
- catch and swallow exceptions in production code to keep a test green.

## Allowed, and reported

- Mechanical test updates the migration rules require (an import path, a
  renamed fixture API), applied the same way as in production code.
- New tests that pin behavior you are about to change (characterization
  tests), added before the change.
- Fixing a test that was already wrong, only with the customer's written
  agreement via `ask_client`, and listed in the report.

## Evidence

- `run_tests` logs every run; cite the run id after each step.
- The pass count must not drop below the baseline run's.
- Run `audit_diff` on the exported patch before submitting; resolve every
  flag or explain it (a flagged patch fails acceptance).
- Flaky test? Re-run once. If it flips, record both run ids and report it as
  pre-existing flakiness; do not mark it skipped.
