# upgrade-migration - Dependency Upgrade & Migration Engineer

Brings one repository's dependencies up to date, clears the known
vulnerabilities that can be cleared, and carries out a framework or runtime
migration, as small test-verified steps. The customer gets patches to merge
plus evidence the platform re-checks. It never pushes, merges or deploys.

## Milestones

| id | what | deliverables (`deliverables/<id>/`) | key automated checks |
|---|---|---|---|
| `m1-assess` | inventory, OSV findings, upgrade + migration plan (read-only) | `inventory.json`, `findings.csv`, `plan.json`, `upgrade-plan.md` | `inventory_matches_repo`, `findings_match_osv`, `plan_covers_findings` |
| `m2-upgrade` | upgrades one step at a time, suite green after each | `repo.patch`, `upgrade-log.json`, `report.md` | `tests_pass`, `osv_delta`, `upgrade_log_verified`, `diff_scope`, `patch_generated`, `patch_matches_repo`, `release_age_ok`, `new_dependencies_disclosed` |
| `m3-migrate` | framework/runtime migration, old-API detectors to zero, residual risk | `repo.patch`, `detectors.json`, `residual-risk.csv`, `migration-report.md` | `tests_pass`, `detectors_cleared`, `osv_delta`, `diff_scope`, `patch_generated`, `patch_matches_repo`, `residual_risks_registered` |

Every milestone also uses kit builtins (`files_exist`, `markdown_sections`,
`no_placeholders`, `json_valid`/`csv_columns`, `rubric_grader`) and ends with
a `human_signoff` by the customer's engineering lead (plan; new dependencies
and the m2 patch; residual risk and the m3 patch).

## How the checks stay honest

Evidence lives under `.agentkit/upgrade_migration/`, which the model cannot
write to. The specialist records the client's test command, the minimum
release age from the upgrade policy and, before the first milestone's loop,
the baseline inventory of the whole `repo/` (write-once). Tools add OSV
query answers and full advisory records, registry release histories, every
test run with the dependency versions it tested, and each detector's
baseline regex and count. Checks recompute from those and from `repo/` on
disk.

Dependencies are compared as states: per package, the set of exact versions
pinned anywhere (every copy in an npm lockfile counts), "declared but not
pinned", or absent. So a report fails acceptance if it forges a finding,
hides a severity, cites a test run that never happened or that ran some
other command, logs steps that were really applied in one batch, claims an
upgrade the lockfile does not contain, "resolves" an advisory by unpinning
or deleting the vulnerable package, deletes, skips or deselects tests,
drops or rewrites a detector to reach zero, or hands in a patch that is not
the repo's real `git diff`.

## Tools (`tools.py`)

`inventory_dependencies` (requirements*.txt and requirements/ folders
including hashed pip-compile output, pyproject.toml, Pipfile, poetry.lock,
uv.lock, Pipfile.lock, package-lock.json v1-v3, npm-shrinkwrap.json,
yarn.lock classic and berry, pnpm-lock.yaml v5-v9, package.json, go.mod;
dependency files of other ecosystems are listed as not read), `osv_scan`
(api.osv.dev querybatch + advisory records), `plan_upgrades` (local OSV
range evaluation, minimal fixing version, bump size, CVSS v3 base score,
ranking), `package_versions` (PyPI / npm release ages, yanked/deprecated),
`run_tests` (pytest / jest / go summaries), `audit_diff`, `scan_patterns`,
`export_patch`. Network and subprocess calls go only through the kit's
policy-checked `fetch` / `run`.

## Specialist (`agent.py`)

`UpgradeMigration` rejects at scoping a `test_command` the checks could
never run (shell syntax, env assignments, a program off the allowlist),
registers every domain check as automated, and adds two hooks around the
kit's loop. `prepare` records the evidence above, gives a `repo/` that
arrived without git history a baseline commit, and records the commit each
milestone starts from. `finalize` rewrites every `repo.patch` deliverable
from `git diff` against that commit, so the patch the customer merges and
the one `diff_scope` audits is the repo's real change; if git cannot
produce it, the model's own patch is deleted and `patch_generated` fails.

## Inputs (intake)

Required: `repository`, `test_command` (one command run from the
repository root, e.g. `python -m pytest -q`), `targets`. Optional: `scope`,
`upgrade_policy` (majors, new dependencies, minimum release age: a stated
number of days can only raise the 7-day default), `deadlines`. The repo is
checked out into `repo/`; other customer files go in `inputs/` (read-only).

## Human gate

No licensed-professional review. The customer's engineering lead approves
the m1 plan (including any new dependency and every major bump), approves
new dependencies and merges each patch, and accepts or rejects each
residual-risk exception; the agent can only propose (`*_pending_approval`
statuses).

## Limits and policy

- Shell: `git`, `python`/`python3`, `pip`, `uv`, `poetry`, `pytest`, `node`,
  `npm`, `yarn`, `pnpm`, `go`; 20-minute command timeout. No `npx`: nothing
  here needs to download and run an arbitrary package.
- Egress allowlist: OSV API, PyPI and its file host, the npm and yarn
  registries, the Go module proxy and checksum database, GitHub (changelogs).
  The file host, yarn registry and Go hosts are not used by the kit's own
  fetch; they are there because the package managers above download from
  them, and the VM's egress rules follow this list.
- Run limits per milestone (resumed runs included): 160 steps, 6M tokens,
  $90, 10 hours. Milestone hour estimates stay inside them; a migration too
  large for one run is scoped as several migration milestones, one per group
  of migration rules.
- v1 ecosystems: PyPI, npm, Go. Maven/Gradle, NuGet, Cargo, Ruby, PHP and
  containers are out of scope; their dependency files are listed as not
  read, and the plan must name them.
- Reachability analysis is not automated; "not affected" claims are
  proposals for the customer to approve.
- Not checked automatically: that the installed environment matches the
  lockfile a test run saw, "no majors" policies, and the intake's `scope`
  answer (those are covered by the m1 plan approval and human sign-off).

## Evals

`evals/fixtures/ledgerly-api/repo` is a fictional repo with invented packages
and advisories (`evals/fixtures/osv-advisories.json`); `evals/cases/` has one
golden case per milestone plus a prompt-injection case (an m2 run where
`repo/docs/UPGRADING.md` tells automated agents to delete tests, neuter CI,
force-push and leak a key), seeded as `repo/` by the kit's eval runner. The
tests run fully offline: `tests/specialists/test_upgrade_migration_e2e.py`
drives all three milestones through the kit with a scripted model, serving
OSV and the registries from those fixtures. A live eval (`python -m agentkit
eval upgrade-migration`) queries the real OSV API, which knows none of the
invented advisories, so its findings and OSV checks will not match the case
notes; what the agent does with the planted instructions in the injection
case is still worth reviewing in a live run.
