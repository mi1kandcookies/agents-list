# Framework and runtime migrations

## Rules before rewrites

A migration is a set of decisions ("old API X becomes Y", "implicit
behavior Z must be made explicit"). Decide each one once, write it down as a
rule, and apply it everywhere the same way. Two call sites translated two
different ways is the most common source of review churn.

Each rule has:
- `id` - short kebab-case name;
- what is retired and what replaces it, with the migration-guide source;
- a detector: a regex (and file glob) that matches the old usage and does
  not match the new one;
- edge cases and when to stop and ask.

## Detectors

- Test every detector with `scan_patterns` before changing code. It must
  find the known usages (non-zero baseline). A detector that matches
  nothing is a broken regex, not a finished migration.
- Do not edit a detector after its baseline is recorded to make the count
  drop; checks compare the regex with the baseline. If a detector was wrong,
  add a corrected one with a new id and explain it in the report.
- Detectors must be narrow enough that the new code does not match.

## Order of work

- Leaf-first: change modules nothing else in the repo imports before the
  modules that import them, so each batch can be tested on its own.
- Batch by rule when a rule is mechanical (rename, import move); batch by
  module when changes interact.
- Run the full suite after each batch; keep a green run between batches.

## Runtime upgrades (Python, Node, Go)

- Change the version declaration the repo actually uses (e.g.
  `requires-python`, `.python-version`, `engines.node`, `.nvmrc`, `go`
  directive in go.mod) - but not CI workflow files; list needed CI changes
  in Follow-ups for the customer.
- Check for removed stdlib modules and changed defaults in the release
  notes of each skipped version, not only the target version.
- Add detectors for removed modules/functions (for example imports of a
  module the target runtime dropped).

## When to stop and ask

- A behavior change is visible to users or API clients.
- A schema, data or config-format migration is required.
- The rule would need different translations in different places.
- The test suite does not exercise the code being migrated: propose
  characterization tests as a scoped change before migrating that module.
