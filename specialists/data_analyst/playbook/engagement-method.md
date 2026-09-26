# Engagement method

How each milestone is run. Deliverable paths are fixed by the manifest.

## M1 - Data inventory and quality profile (`m1-profile`)

1. `list_tables`, then `profile_tables` for every table (the default), with
   the intake's `sensitive_columns` as `mask_columns`.
2. For each table, work out: what one row means (the grain), the key, how it
   joins to the others, the date range covered, and how fresh it is.
3. Walk the data-quality checklist (`playbook/data-quality-checklist.md`)
   and confirm each suspected issue with a query before writing it down.
   Quantify it: "1 of 62 invoice rows is an exact duplicate", not "some
   duplicates".
4. Write `data-quality.md` with these sections:
   - `## Inventory` - one short entry per table: source file, grain, key,
     row count, date range, joins.
   - `## Data quality issues` - one entry per issue: what, how many rows,
     impact on metrics, proposed handling (default position).
   - `## Open questions` - what only the customer can settle; the default
     you will use until they answer.
5. Do not change `profile.json` by hand; re-run `profile_tables` instead.

## M2 - Metric definitions (`m2-metrics`)

1. Agree the KPI list from intake. For each KPI write one saved query
   (`save_query`, milestone `m2-metrics`) returning a single row with a
   numeric `value` column for the reference period.
2. Record the definition in `metrics.yaml` (see
   `playbook/metric-definitions.md`).
3. Run `reconcile_metrics`. A mismatch means the definition is wrong or the
   reference is built differently; find which. Never tune a query to hit a
   number without a business rule that explains the change, and never write
   a reference number into the SQL (acceptance refuses it). The reference
   file is not a table; read it with `read_file`. If the customer supplied no
   reference totals, every metric is marked `no_reference`: say in
   `definitions.md` that nothing was reconciled and ask for reference numbers.
4. Write `definitions.md`: `## Metrics`, `## Business rules applied`,
   `## Reconciliation` (a table from `reconciliation.csv`, with an
   explanation for any gap), `## Open questions`.
5. The customer's metric owner signs off the definitions (human check).

## M3 - Analysis report (`m3-analysis`)

1. Break each business question into parts; save one query per part.
2. Reuse M2 definitions when they exist; copy their filters verbatim rather
   than re-deriving them.
3. `record_figure` for every number you intend to write, including
   percentages and differences (compute them in SQL).
4. Write `report.md`:
   - `## Summary` - three to five sentences answering the questions.
   - `## Findings` - one subsection per question; each claim followed by the
     figure and its `[F:id]` marker.
   - `## Method` - data used, definitions applied, exclusions, query names.
   - `## Caveats` - data-quality issues that affect the answers, what was not
     checked, and the approval note for externally reported numbers.
5. Before submitting, read the report once more and confirm every number has
   a marker directly after its display text. Money, percentages, decimals,
   numbers with thousands separators and whole numbers of five or more
   digits without a marker fail acceptance anywhere but `## Method`.
