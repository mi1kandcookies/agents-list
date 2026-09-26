# Engagement method

Three milestones. Each ends with deliverables under
`deliverables/<milestone-id>/` and a call to `submit_milestone`.

## m1-assess - inventory, findings, plan (read-only)

1. `run_tests` with the intake's test command exactly as given (same argv,
   `cwd: repo`), label `baseline`. Red? Ask. Only runs of that command in
   `repo/` count as evidence, and the baseline is the best such run at the
   dependency versions the repo arrived with.
2. `inventory_dependencies` with `output: deliverables/m1-assess/inventory.json`.
   Note unpinned dependencies: they cannot be scanned exactly; recommend
   pinning (lockfile) as an upgrade step. Rows with ecosystem `unknown` are
   dependency files the tools cannot read (another ecosystem, or broken):
   name each path in the plan's Inventory section as not scanned.
3. `osv_scan`, then `plan_upgrades` with
   `findings_csv: deliverables/m1-assess/findings.csv` and
   `output: deliverables/m1-assess/plan.json`.
4. For each planned bump, `package_versions` to confirm the target exists, is
   old enough, and to see whether a newer patch release of the same line is a
   better target.
5. For each minor/major bump, skim the changelog between current and target
   and list breaking changes that touch code in `repo/` (`search_files` for
   the affected APIs).
6. If the intake names a framework/runtime target, draft **migration rules**:
   one row per retired API or behavior, with its replacement and a detector
   regex. These become m3's `detectors.json`.
7. Write `upgrade-plan.md` with sections: Summary, Inventory, Findings,
   Upgrade steps, Migration rules, Risks, Exceptions, Questions. Every
   advisory id from `findings.csv` must appear either in an upgrade step or
   in Exceptions (no fix available, unreachable, dev-only - with reasoning).

## m2-upgrade - small, test-verified steps

For each step in the approved order:
1. Change the version with the ecosystem's tool (`pip`/`uv`/`poetry lock`,
   `npm install --ignore-scripts <pkg>@<ver>` or `--package-lock-only`,
   `yarn`/`pnpm` equivalents, `go get <mod>@<ver>`), so lockfiles stay
   consistent. Keep lifecycle scripts off: a new release's install script is
   exactly what the release-age rule guards against.
2. Adapt call sites if needed; cite the changelog source for each change.
3. `run_tests` with the client's command (label `step-N <package> <version>`).
   Green -> log the step with the run id. Red -> fix or revert; never weaken
   tests.

Then: `osv_scan` again, `export_patch` to `deliverables/m2-upgrade/repo.patch`,
`audit_diff` on it, write `upgrade-log.json` and `report.md` (Summary, Steps,
Vulnerability delta, Code changes, New dependencies, Follow-ups), and commit
locally.

`upgrade-log.json`:

```json
{"steps": [{"ecosystem": "PyPI", "name": "example-lib", "from": "2.2.0",
            "to": "2.4.1", "test_run": "run-2", "changelog": "S1",
            "notes": "renamed option x -> y in app/config.py"}]}
```

How the log is checked (`upgrade_log_verified`):
- Every version change of a package the repo had at baseline is a step,
  including lockfile-only transitive changes an upgrade pulled in, each copy
  of a package held at several versions, and removals.
- Replaying the steps from the baseline must give the repo's versions now.
  Each run's recorded dependency versions must show its step applied and no
  later step: change, test, log; never change everything and log afterwards.
- A cited run must be green with no fewer passing and no more skipped tests
  than the baseline run.
- Steps that had to move together (a package and the transitive versions it
  pulled in, a coupled group) are consecutive entries citing the same run.
- `"from": "unpinned"` pins a package that had no exact version;
  `"to": "removed"` takes a package out (say why in the report). A package
  cannot end unpinned: unpinning a vulnerable package resolves nothing.

## m3-migrate - framework / runtime migration

1. Write `deliverables/m3-migrate/detectors.json` from the approved
   migration rules and run `scan_patterns` on it **before** changing code.
   A detector with a zero baseline proves nothing; fix its regex first.
   Every detector ever scanned with a non-zero baseline must stay in the
   file: active (driven to zero) or, if it was wrong, as
   `{"id", "superseded_by": "<active id>", "reason"}`.
2. Work leaf-first (modules with no internal dependents first). After each
   batch: `run_tests`, `scan_patterns`.
3. When all detectors read zero and tests are green: `osv_scan`,
   `export_patch`, `audit_diff`.
4. `residual-risk.csv` (vuln_id, package, status, justification, review_by)
   for every advisory still open; status one of `no_fix_available`,
   `deferred`, `accepted_pending_approval`, `not_affected_pending_approval`.
5. `migration-report.md`: Summary, What changed, Verification, Residual risk,
   Rollback, Follow-ups.
