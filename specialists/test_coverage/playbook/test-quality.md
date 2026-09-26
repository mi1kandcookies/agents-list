# Test quality bar

A test is worth keeping when it would fail if the code under test were
wrong, passes every time otherwise, and a maintainer can tell from its name
what broke.

## Must

- Assert a specific outcome (value, error type and message, state change).
- Exercise the real unit; fake only process boundaries (network, clock,
  randomness, filesystem, subprocess) and slow external services.
- Pass alone, in its file, and in varied order.
- Follow the repository's framework, fixture conventions, layout and naming.
- Name the behavior and condition: `test_rejects_expired_coupon`.

## Must not

- Assert nothing, assert constants (`assert True`) or compare a value with
  itself.
- Mock the function or class being tested, or patch its internals.
- Snapshot a whole large object when two fields carry the meaning.
- Sleep for timing; wait on a condition or inject the clock.
- Reach the network or real third-party APIs (sandbox fakes only).
- Depend on another test's side effects or on execution order.
- Skip, delete or loosen existing tests.

## Flaky tests found along the way

Existing flaky or broken tests go in the report with the runs they failed
in and a likely cause (order dependence, time, shared state, network,
resource leaks). Do not quarantine or fix them unless the SOW asks; the
census is the deliverable.

## Mutation score

Mutation testing changes the code in small ways (flip a comparison, drop a
statement, change a constant) and re-runs the tests. A surviving mutant is a
behavior nothing asserts. Scope runs to the target files and time-box them;
record the tool, version and options in the report. Score =
detected / (detected + survived + no coverage); compile errors and ignored
mutants do not count either way.

## Runtime budget

Measure suite wall time before and after (the `matrix.json` logs help).
If new tests push growth past the budget, speed up fixtures or move slow
cases behind the repository's existing slow-test marker, and say so.
