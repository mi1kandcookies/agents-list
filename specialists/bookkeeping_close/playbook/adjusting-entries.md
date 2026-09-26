# Adjusting entries

Every entry is a draft with status "draft - do not post". A human approves
and posts it. `draft_journal_entry` refuses unbalanced entries, entries
without support, suspense accounts and descriptions that read like a plug.

## Schedules (`build_accrual_schedule`)

Straight line: amount due through period end = total x months elapsed /
months (the full total in the last month), this-period entry = due minus
booked to date. Catching up a missed prior month is automatic and should be
called out in the memo.

| type | debit | credit |
|---|---|---|
| prepaid (insurance, software paid annually) | expense | prepaid asset |
| accrual (received, not yet billed) | expense | accrued liability |
| depreciation | depreciation expense | accumulated depreciation |
| deferred_revenue (billed in advance, earned this period) | deferred revenue | revenue |

A negative this-period amount means more was booked than is due: the tool
drafts the reversal; explain why it happened.

## Other entries

- Bank fees and interest from the reconciliation's unrecorded items.
- Reclasses of items the client identified in answer to a question; cite the
  answer.
- Nothing else without support. Estimates (accruals without an invoice) state
  the basis, for example a meter read, a contract rate or last month's bill.

## Entry ids and support

Use `ADJ-<YYYYMM>-<short-name>` ids. Support cites the file and the row or
item id (`inputs/accrual_schedule.csv#PRE-001`,
`bank_reconciliation.json unrecorded row 17`, `client answer 2026-09-03`).

## Needs a human decision

Write-offs, capitalize-vs-expense, owner draws and distributions, anything
tax-sensitive (meals, vehicles, related parties), revenue recognition beyond
simple straight-line deferrals, inventory costing, multi-currency and
intercompany. Queue these with a recommendation instead of drafting them.
