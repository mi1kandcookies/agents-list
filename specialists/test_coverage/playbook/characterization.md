# Characterization tests

Goal: a refactor of the target modules can prove it changed no observable
behavior. The tests describe what the code does, not what it should do.

## Choosing what to pin

- Enter through the public surface the rest of the system uses (functions,
  classes, HTTP handlers, CLI entry points), never private helpers.
- Pin outputs, raised errors and side effects that callers can observe:
  return values, persisted rows, emitted messages, files written, log lines
  that other systems parse.
- Sample inputs deliberately: typical values, boundaries (empty, zero, one,
  maximum), invalid input, unusual encodings. Grow the set until the
  mutation score on the module reaches the floor.

## Making observations deterministic

Before recording an expected value, remove everything that varies between
runs: freeze or inject the clock, seed or inject randomness, sort unordered
collections, normalize generated ids and absolute paths, pin locale and
time zone. If a value cannot be made deterministic, assert its shape
(type, length, pattern) instead of its content and say so in the report.

## Golden files

- Keep them small and named after the case (`invoice_zero_quantity.json`).
- Store them beside the tests; never under production paths.
- No real customer data, personal data or secrets. Build inputs from
  obviously fictional values.

## Suspicious behavior log

When observed behavior looks wrong, do not pin it silently and do not fix
it. Add an entry to `suspicious-behaviors.md`:

```
## S-3: negative quantities produce a credit invoice
- Where: billing/invoice.py:88
- Observed: quantity=-2 returns total -40.00 with status "issued"
- Why suspicious: the order form rejects negatives; invoices elsewhere assume totals >= 0
- Test: tests/characterization/test_invoice.py::test_negative_quantity_observed
- Customer decision: (bug / intended / won't fix)
```

Mark the pinning test with a name suffix (`_observed`), or a custom marker
registered in the tests' own conftest.py, so it is easy to flip once the
customer decides. Never skip or xfail it: it must run and pass like any
other new test.

## Evidence for the report

- `run_test_matrix` with `runs: 10` into `deliverables/m2-characterization/runs`,
  running the whole suite with the same command as M1 (the platform re-runs
  that command after you submit and grades its own runs).
- A mutation report (mutation-testing-elements JSON) scoped to the targets,
  saved as `deliverables/m2-characterization/mutation.json`.
- `export_patch` to `deliverables/m2-characterization/repo.patch`, then
  `diff_scope`, `find_assertion_free_tests`, `find_weakened_tests` and
  `scan_patch_secrets`.
