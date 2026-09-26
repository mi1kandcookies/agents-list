# Bookkeeping & Month-End Close (`bookkeeping-close`)

A first-party specialist that closes a small business's month from CSV
exports and hands the work product to the client's accountant or CPA. It
categorizes bank activity, reconciles the bank account to the cent, drafts
adjusting entries with support, builds the adjusted trial balance, explains
material movements and produces a P&L and balance sheet.

All arithmetic runs in deterministic Decimal code (`tools.py`); the model
decides ambiguous categorizations, traces breaks and writes commentary.
Acceptance checks (`checks.py`) recompute everything from the customer's
files, so a forged or plugged number fails.

## Milestones

| id | deliverables (under `deliverables/<id>/`) | domain checks |
|---|---|---|
| `m1-onboarding` | `diagnostic.md`, `account_map.csv`, `opening_balance_tieout.csv`, `categorization_rulebook.csv`, `close_checklist.md` | `account_map_complete`, `opening_balances_tie` |
| `m2-period-close` | `categorized.csv`, `exceptions.csv`, `bank_reconciliation.json`, `accrual_schedule.csv`, `journal_entries.csv`, `trial_balance.csv` | `categorization_complete`, `bank_rec_ties`, `journal_entries_balanced`, `trial_balance_ties`, `no_plugs` |
| `m3-close-package` | `flux.csv`, `financial_statements.json`, `close_package.md` | `trial_balance_ties`, `flux_commentary_complete`, `financial_statements_tie` |

Each milestone also uses kit checks (`files_exist`, `markdown_sections`,
`no_placeholders`, `disclaimer_present`, `csv_columns`, `json_valid`,
`rubric_grader`) and ends with `human_signoff`.

## Inputs (in `inputs/`, read-only)

| file | columns |
|---|---|
| `bank_statement.csv` | date, description, amount (+ in / - out), balance |
| `gl_detail.csv` | entry_id, date, account, description, debit, credit (opening balances dated before the period) |
| `chart_of_accounts.csv` | account, name, type (asset, liability, equity, revenue, expense) |
| `prior_trial_balance.csv` | account, debit, credit |
| `prior_month_pl.csv` (optional) | account, debit, credit, for P&L flux |
| `accrual_schedule.csv` (optional) | item_id, type, description, pl_account, balance_account, total_amount, start_date, months, booked_to_date, support |
| `categorization_rules.csv` (optional) | pattern, account, confidence |
| `close_parameters.json` | period, cash_account, statement_ending_balance, flux thresholds; written from the intake before the run when the client did not upload one |

## Tools

`parse_bank_statement`, `categorize_transactions`, `find_exceptions`,
`reconcile_bank`, `draft_journal_entry`, `build_accrual_schedule`,
`build_trial_balance`, `flux_analysis`, `tie_opening_balances`,
`build_account_map`, `build_financial_statements`, plus the kit's file,
document, ledger and platform tools. No shell, no network. Every path a
tool is given goes through the kit's policy gate, and every file a tool
writes is recorded as agent-authored, so it can never be cited as a source.

`agent.py` adds two hooks: `validate_intake` treats a period that is not
YYYY-MM as a blocking gap, and `prepare` writes `inputs/close_parameters.json`
from the intake (the materiality answer becomes the flux thresholds), so the
checks recompute against the client's figures, not ones the agent reported.

## Human gate

Required. Reviewer: accountant or CPA. Every journal entry is written with
status `draft - do not post`; the agent never posts to a ledger, pays bills,
moves money, contacts banks or vendors, files returns or closes a period.
Low-confidence categorizations, the exceptions queue, estimates,
capitalization, owner-draw and tax-sensitive reclasses and the period
sign-off all need human approval. The work product is not an audit, review,
compilation or tax advice.

## Limits and scope (v1)

One bank account per run, single entity, single currency, straight-line
schedules. Deferred: live ledger connectors (read-only), card and processor
clearing reconciliations, AR/AP aging ties, multi-entity, inventory costing,
complex revenue recognition, FX. Run limits: 120 steps, 3M tokens, $40,
180 minutes.

## Evals

`evals/fixtures/harbor_lane/` is a synthetic coffee roaster (fictional) with
an August close that reconciles with outstanding checks, deposits in transit,
bank-only fees and interest, a suspense balance and a planted instruction in
a support document. `evals/cases/*.json` describe the expected outcome per
milestone, including a forced-tie case that must fail honestly.
