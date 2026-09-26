# Bank reconciliation

## The equation

    adjusted bank balance = statement ending balance + outstanding items (in the GL, not yet on the statement)
    adjusted book balance = GL ending balance + unrecorded items (on the statement, not yet in the GL)
    unexplained difference = adjusted bank - adjusted book            must be 0.00

`reconcile_bank` computes all of it for the period's bank lines (lines
dated after period end belong to the next close). Your job is to grader
whether each reconciling item is real.

## Carrying the prior reconciliation forward

Checks and deposits outstanding at the prior close clear this month. The
tool takes them from `inputs/prior_bank_reconciliation.json` (last month's
`bank_reconciliation.json`) or a CSV of them; a prior item may clear any
time after its date, and one that still has not cleared stays on the
outstanding list with its age. `opening_difference` is statement opening
plus prior outstanding minus GL opening: non-zero means the prior
reconciliation does not carry forward (a missing prior rec, or last month's
fee entries were never posted). If the client has not uploaded the prior
rec and the difference equals items clearing early in the month, ask for it.

## Matching

Lines match on the exact amount within a date window (default 5 days,
or `date_window` in `close_parameters.json`), closest date first. Then a bank
deposit can clear several receipts recorded separately (customer checks
deposited together), and one book entry can clear several payouts from the
same payer; these appear in `batched_matches`, and each should agree with a
deposit slip or processor report. Amount breaks are never matched: a
$4,150.00 check against a $4,105.00 clearing is two items and a question.

## Reading the result

- **Outstanding payment** (negative, GL only): a check or payment recorded
  before it cleared. Fine near period end; older than 60 days means it may be
  stale, voided or recorded twice. Say which.
- **Deposit in transit** (positive, GL only): recorded but not yet deposited.
  More than a few business days old is a red flag for a missing or misposted
  deposit.
- **Bank charge / bank credit** (statement only): fees, interest, returned
  items, transfers nobody recorded. Each needs a draft entry that cites the
  statement row; the check re-runs the reconciliation with your drafts and
  expects no unrecorded items left and nothing newly outstanding. Before
  drafting a deposit, make sure it is not already in the books under other
  amounts: a duplicate receipt fails the check.
- **Non-zero unexplained difference**: the opening does not carry forward
  (see `opening_difference`) or the statement CSV is broken (running
  balance breaks). Trace it; never book a "reconciling adjustment".

## Likely causes for a break

Work through: timing (dates just across period end), transposition (the
difference divides by 9), duplicate or missing entries, sign flips (the
difference is twice an item), fees netted from deposits (processor payouts),
and posting to the wrong cash account. Record the hypothesis and the
evidence in the exceptions queue.

## Statement integrity

`parse_bank_statement` reads oldest-first and newest-first exports and
reports rows where the running balance does not follow from the activity.
A break usually means rows are missing or out of order in the export: ask
the client for a fresh export and the ending balance printed on the
statement. Do not suggest the file was tampered with.
