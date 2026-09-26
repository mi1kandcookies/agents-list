# Tamarind Loop Software (fictional) - analysis request

We sell a subscription product on three plans (starter, growth, scale) and
invoice monthly. The exports in this folder come from our billing system:

- `customers.csv` - one row per customer account. `is_test = 1` marks our own
  QA and demo tenants. `deleted_at` is set when an account is closed.
- `invoices.csv` - one row per invoice. Only `status = paid` counts as
  revenue; `void` and `refunded` invoices do not.
- `reference_totals.csv` - numbers our finance lead reported for closed
  months. Your metric definitions should tie to these.

Questions for the analysis milestone:

1. How did paid revenue move from June to August 2026, and which plan drove it?
2. Which regions contribute the most paid revenue in August 2026?
3. How many paying customers did we have each month, and did we lose any?
4. Anything in the data we should fix before trusting a dashboard?
