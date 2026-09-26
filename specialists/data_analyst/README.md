# Data Analyst specialist

Slug `data-analyst`, package `specialists.data_analyst`, profile `data`.

A fixed-scope analytics engagement for teams without a data team. The agent
works over the customer's CSV exports and SQLite databases, writes metric
definitions that tie to numbers the customer already trusts, and answers
business questions in a report where every number can be re-run.

## Milestones

| id | deliverables | accepted when |
|---|---|---|
| `m1-profile` | `profile.json`, `data-quality.md` | every in-scope table is profiled and the counts match a fresh profile of `inputs/` (`profile_matches_source`); write-up has Inventory / Data quality issues / Open questions; rubric |
| `m2-metrics` | `metrics.yaml`, `queries/`, `results/`, `reconciliation.csv`, `definitions.md` | every definition is complete and runs (`metrics_valid`); saved queries reproduce their results (`queries_reexecute`); each metric with a reference total ties within tolerance and `reconciliation.csv` matches the recomputation (`metrics_reconcile`); metric owner signs off (human) |
| `m3-analysis` | `report.md`, `figures.json`, `queries/`, `results/` | saved queries reproduce their results; every `[F:id]` figure recomputes from its query and its display text is on the citing line (`figures_match_queries`); required sections, no placeholders, word count; rubric |

All deliverables live under `deliverables/<milestone-id>/`.

## How numbers stay honest

- Each tool call loads `inputs/` into a fresh in-memory SQLite session, then
  locks it with `PRAGMA query_only` and an authorizer that only allows
  reads. A text guard also rejects anything but a single `SELECT`/`WITH`
  statement, so the model gets a clear error.
- `save_query` stores the SQL and its full result; `record_figure` takes a
  value from a saved query and returns the exact display text the report
  must use, cited as `[F:id]`.
- Checks never read the agent's numbers as truth: they re-profile, re-run
  and recompute from `inputs/`, so a hand-edited `profile.json`, result CSV,
  figure or reconciliation fails.

## Inputs (intake)

`data_files` (required), `business_questions` (required), `kpis`
(required), `reference_totals` (optional `inputs/reference_totals.csv` with
`metric,value,tolerance_pct,note`), `business_rules`, `sensitive_columns`.

## Tools

Kit: `read_document`, `read_file`, `write_file`, `edit_file`, `list_files`,
`search_files`, `ask_client`, `post_progress`, `submit_milestone`.
Domain (`tools.py`, `TOOL_DEFS`): `list_tables`, `profile_tables`,
`run_query`, `save_query`, `record_figure`, `reconcile_metrics`.
Domain checks (`checks.py`, `CHECK_DEFS`): `profile_matches_source`,
`queries_reexecute`, `figures_match_queries`, `metrics_valid`,
`metrics_reconcile`.

`agent.py` holds the `DataAnalyst` Specialist subclass; the kit's base class
wires the manifest, `TOOL_DEFS` and `CHECK_DEFS`. Its `finalize` hook adds
each milestone's `queries/*.sql` and `results/*.csv` to the submitted
artifacts, so the evidence hash covers the SQL that acceptance re-runs.
Domain tools resolve every file they write, and any path the model picks,
through the kit's `resolve_path` (policy-checked; written files are marked
agent-authored).

## Human gate

No licensed-professional review (`human_gate.required: false`). The
customer's metric owner signs off the M2 definitions, and numbers used in
board, investor, lender or regulatory reporting need the finance owner's
approval first. The agent never publishes, e-mails or writes to the
customer's systems.

## Limits

No network egress, no shell commands. 120 steps, 3M tokens, $30 and
180 minutes per milestone run. v1 reads CSV and SQLite only; warehouse
connectors (Postgres, Snowflake, BigQuery), notebooks and dashboards are
later work.

## Evals

`evals/fixtures/tamarind-loop/` is a synthetic billing export for a
fictional company, seeded with traps (an exact duplicate invoice row, test
tenants, a soft-deleted account, a missing region). `evals/cases/*.json`
holds one case per milestone.

## Running

```bash
python -m agentkit validate data-analyst
python -m agentkit estimate data-analyst --intake intake.json
python -m agentkit run data-analyst --brief brief.json --milestone m1-profile --workspace WS
python -m agentkit check data-analyst --milestone m1-profile --workspace WS
```

`tests/specialists/test_data_analyst_e2e.py` runs every milestone offline
(a scripted model driving the real tools on a copy of the eval fixture) and
checks that forged profiles, covered-up reconciliations and misstated or
hand-edited figures come back `needs_revision`.
