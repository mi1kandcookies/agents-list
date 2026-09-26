# Close method

The close is a sequence where each step only runs on numbers the previous
step proved. Skipping ahead produces statements built on unreconciled cash.

## Milestone m1-onboarding: books diagnostic

1. `build_account_map`: every GL account lands on a statement and a line.
   Refine the default line names (for example split Operating expenses into
   Payroll, Occupancy, Software) but keep the statement the type dictates.
   GL accounts missing from the chart of accounts are a finding, not something
   to fix silently.
2. `tie_opening_balances` for the first period: GL balances before the period
   must equal the prior closing trial balance within $1. Explain each
   difference in the `explanation` column from evidence (late entries,
   reclasses after the export); if you cannot, ask the client.
3. Write `categorization_rulebook.csv` (`pattern, account, confidence`): one
   row per recurring payee seen in the statement or GL, a case-insensitive
   regular expression anchored where possible, and a confidence that reflects
   how unambiguous the payee is. Checks and transfers are never above 0.7.
4. Write `close_checklist.md`: the entity's close steps in order, the owner of
   each (agent, client, reviewer) and the day it is due.
5. Write `diagnostic.md` with the sections Summary, Chart of accounts,
   Opening balances, Categorization rules, Open questions, plus the disclaimer.

## Milestone m2-period-close: workpapers

Order: categorize, exceptions, reconcile, schedules, other entries, trial
balance. Re-run `build_trial_balance` after the last entry is drafted.

Default positions (change only with the reviewer's written policy):

- Materiality for flux: changes of at least $1,000 and 10%.
- Categorization confidence below 0.80 goes to review.
- Round-dollar movements of $1,000 or more are confirmed, not assumed.
- Items outstanding more than 60 days are listed individually with a reason.
- Personal-looking spend (peer-to-peer apps, retail on a business card) is
  queued, never reclassified to owner draw without approval.
- Payees are never merged automatically; near-duplicates are flagged.

## Milestone m3-close-package

1. `flux_analysis` against the prior period (balance-sheet accounts: balance
   against the prior closing trial balance; income-statement accounts: this
   month's activity against the prior month's P&L, never year to date), then
   write commentary for every flagged row from the underlying activity: name the entries, invoices or
   schedule items that explain the movement. "Timing" alone is not an
   explanation; say what the timing was.
2. `build_financial_statements` from the adjusted trial balance.
3. `close_package.md` with the sections Summary, Reconciliations, Adjusting
   entries, Flux commentary, Financial statements, Open items, Reviewer
   sign-off, plus the disclaimer. The Open items section repeats every
   exception still unresolved; the Reviewer sign-off section lists what the
   accountant or CPA must approve before posting.
