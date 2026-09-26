# Dependency upgrades: default positions

## Choosing a target version

- Start from `plan_upgrades`: the smallest version that fixes every
  advisory with a known fix. Moving further (to latest) is a separate,
  optional step, and only when the customer asked for "latest".
- Prefer the newest **patch** release of the fixing minor line: same risk,
  more fixes.
- Minimum release age: 7 days unless the intake says otherwise. A brand-new
  release is the usual vector for a hijacked package; a critical fix
  published yesterday is the one exception, and then only with the customer's
  explicit go-ahead in writing.
- Never choose a yanked or deprecated version.
- 0.x packages: a minor bump is breaking; treat it like a major.

## Risk tiers (present these in the plan)

| tier | bump | expectation |
|---|---|---|
| low | patch | lockfile change only; suite green |
| medium | minor | read the changelog; small call-site changes possible |
| high | major / 0.x minor | migration guide; call-site changes; ask before starting if not in the approved plan |

## Transitive dependencies

- Fix a vulnerable transitive by upgrading the direct parent that pulls it
  in, when a parent release exists that requires the fixed version.
- Otherwise use the ecosystem's override mechanism (npm `overrides`, pip
  constraints file, Go `require` of the fixed module) and say so in the
  report: overrides are debt the customer must know about.

## Exceptions (things you may not fix)

Put an advisory in Exceptions/residual risk, with reasoning, when:
- no fixed version exists;
- the only fix is a major bump the customer did not approve;
- the package is dev-only and never shipped (still list it);
- the vulnerable function is not reachable from the code (say how you
  checked: `search_files` for the affected symbol, entry points).
Unreachability is a claim the customer must approve; never mark it approved.

## Evidence discipline

- Advisory facts (affected ranges, fixed versions, severities) come from
  `osv_scan` results, not memory.
- Release dates come from `package_versions`.
- Breaking-change claims cite the changelog/migration guide you fetched and
  recorded with `record_source`.
