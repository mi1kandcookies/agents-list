# Metric definitions

`deliverables/m2-metrics/metrics.yaml` is the single place a metric is
defined. It is meant to be reviewed like code and reused by later analyses
and dashboards.

```yaml
metrics:
  - name: paid_revenue_2026_08        # matches the reference_totals.csv metric name
    description: Paid invoice revenue in August 2026, test tenants excluded
    grain: month                       # what one value summarizes
    query: queries/paid_revenue_2026_08.sql   # relative to deliverables/m2-metrics/
    value_column: value                # column of the first result row
    owner: Finance lead                # who approves the definition
    filters:                           # human-readable restatement of the WHERE clause
      - invoices.status = 'paid'
      - customers.is_test = 0
      - duplicate export rows removed
    notes: Refunds are recorded as a separate status and are excluded.
```

Required fields: `name`, `description`, `grain`, `query`, `value_column`,
`owner`. Names are unique, lowercase, and match the customer's reference
metric names when a reference exists.

Default positions when the customer has not said otherwise:

- Revenue is recognized on paid invoices only, in the invoice's month.
- Test, demo and internal accounts are excluded from every business metric.
- Exact duplicate export rows are removed before aggregation.
- Counts of customers are distinct customers, not rows.
- Money is rounded to cents only at the end (`ROUND(SUM(x), 2)`).
- Tolerance to a reference is 0.5% for money and exact for counts, unless
  `reference_totals.csv` gives its own `tolerance_pct`.

If a metric cannot reconcile, do not force it. Write down both numbers, the
difference, the rule you think explains it, and the question for the
customer.
