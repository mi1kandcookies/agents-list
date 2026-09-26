# Bank reconciliation

## The equation

    adjusted bank balance = statement ending balance + outstanding items (in the GL, not yet on the statement)
    adjusted book balance = GL ending balance + unrecorded items (on the statement, not yet in the GL)
    unexplained difference = adjusted bank - adjusted book            must be 0.00

`reconcile_bank` computes all of it. Your job is to grader whether each
reconciling item is real.

## Matching

Lines match on the exact amount within a date window (default 5 days),
closest date first. Amount breaks are never matched: a $4,150.00 check
against a $4,105.00 clearing is two items and a question.

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
  expects no unrecorded items left.
- **Non-zero unexplained difference**: the opening balances do not agree
  (prior rec left something open) or the statement CSV is broken (running
  balance breaks). Trace it; never book a "reconciling adjustment".

## Likely causes for a break

Work through: timing (dates just across period end), transposition (the
difference divides by 9), duplicate or missing entries, sign flips (the
difference is twice an item), fees netted from deposits (processor payouts),
and posting to the wrong cash account. Record the hypothesis and the
evidence in the exceptions queue.

## Statement integrity

`parse_bank_statement` reports rows where the running balance does not follow
from the activity. A break means the export is incomplete or edited: ask the
client for a fresh export and the ending balance printed on the statement.
