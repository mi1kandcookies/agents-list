# upgrade-migration - Dependency Upgrade & Migration Engineer

Brings one repository's dependencies up to date, clears the known
vulnerabilities that can be cleared, and carries out a framework or runtime
migration, as small test-verified steps. The customer gets patches to merge
plus evidence the platform re-checks. It never pushes, merges or deploys.

## Milestones

| id | what | deliverables (`deliverables/<id>/`) | key automated checks |
|---|---|---|---|
| `m1-assess` | inventory, OSV findings, upgrade + migration plan (read-only) | `inventory.json`, `findings.csv`, `plan.json`, `upgrade-plan.md` | `inventory_matches_repo`, `findings_match_osv`, `plan_covers_findings` |
| `m2-upgrade` | upgrades one step at a time, suite green after each | `repo.patch`, `upgrade-log.json`, `report.md` | `tests_pass`, `osv_delta`, `upgrade_log_verified`, `diff_scope`, `patch_matches_repo`, `release_age_ok`, `new_dependencies_disclosed` |
| `m3-migrate` | framework/runtime migration, old-API detectors to zero, residual risk | `repo.patch`, `detectors.json`, `residual-risk.csv`, `migration-report.md` | `tests_pass`, `detectors_cleared`, `osv_delta`, `diff_scope`, `patch_matches_repo`, `residual_risks_registered` |

Every milestone also uses kit builtins (`files_exist`, `markdown_sections`,
`no_placeholders`, `json_valid`/`csv_columns`, `rubric_grader`); m1 and m3
end with a `human_signoff` by the customer's engineering lead.

## How the checks stay honest

Tools record evidence under `.agentkit/upgrade_migration/`, which the model
cannot write to: the baseline dependency list (write-once), OSV query
answers and full advisory records, registry release histories, a log of
every test run, and each detector's baseline regex and count. Checks
recompute from those and from `repo/` on disk, so a report that forges a
finding, hides a severity, cites a test run that never happened, claims an
upgrade the lockfile does not contain, deletes or skips tests, or rewrites a
detector to reach zero fails acceptance.

## Tools (`tools.py`)

`inventory_dependencies` (requirements*.txt, poetry.lock, Pipfile.lock,
package-lock.json v1-v3, package.json, go.mod), `osv_scan` (api.osv.dev
querybatch + advisory records), `plan_upgrades` (local OSV range
evaluation, minimal fixing version, bump size, CVSS v3 base score, ranking),
`package_versions` (PyPI / npm release ages, yanked/deprecated),
`run_tests` (pytest / jest / go summaries), `audit_diff`, `scan_patterns`,
`export_patch`. Network and subprocess calls go only through the kit's
policy-checked `fetch` / `run`.

## Specialist (`agent.py`)

`UpgradeMigration` registers every domain check as automated and adds two
hooks around the kit's loop. `prepare` records the intake's `test_command`
(what `tests_pass` re-runs), gives a `repo/` that arrived without git
history a baseline commit, and records the commit each milestone starts
from. `finalize` rewrites every `repo.patch` deliverable from `git diff`
against that commit, so the patch the customer merges and the one
`diff_scope` audits is the repo's real change.

## Inputs (intake)

Required: `repository`, `test_command`, `targets`. Optional: `scope`,
`upgrade_policy` (majors, new dependencies, minimum release age),
`deadlines`. The repo is checked out into `repo/`; other customer files go in
`inputs/` (read-only).

## Human gate

No licensed-professional review. The customer's engineering lead approves
the m1 plan (including any new dependency and every major bump), merges each
patch, and accepts or rejects each residual-risk exception; the agent can
only propose (`*_pending_approval` statuses).

## Limits and policy

- Shell: `git`, `python`/`python3`, `pip`, `uv`, `poetry`, `pytest`, `node`,
  `npm`, `npx`, `go`; 20-minute command timeout.
- Egress allowlist: OSV API, PyPI, npm registry, Go module proxy/sumdb,
  GitHub (changelogs).
- Run limits: 160 steps, 6M tokens, $90, 8 hours per milestone.
- v1 ecosystems: PyPI, npm, Go. Maven/Gradle, NuGet, Cargo and containers
  are out of scope for automated checks.
- Reachability analysis is not automated; "not affected" claims are
  proposals for the customer to approve.

## Evals

`evals/fixtures/ledgerly-api/repo` is a fictional repo with invented packages
and advisories (`evals/fixtures/osv-advisories.json`); `evals/cases/` has one
golden case per milestone, seeded as `repo/` by the kit's eval runner. The
tests run fully offline: `tests/specialists/test_upgrade_migration_e2e.py`
drives all three milestones through the kit with a scripted model, serving
OSV and the registries from those fixtures. A live eval (`python -m agentkit
eval upgrade-migration`) queries the real OSV API, which knows none of the
invented advisories, so its findings will not match the case notes.
