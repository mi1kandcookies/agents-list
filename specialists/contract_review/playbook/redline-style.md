# Redline style

- **Minimal edits.** Change the words that carry the risk ("three (3)" to
  "twelve (12)", "paid" to "paid or payable"), not the sentence around them.
  `build_redline` diffs each op word by word, so an op that retypes a whole
  clause still produces a small tracked change, but only if the rest of the
  text is copied exactly.
- **One issue, one op where possible.** Each op names its `issue_id`, so
  the attorney can accept or reject per issue.
- **Keep their drafting.** Reuse the contract's defined terms, numbering
  and tone. Do not introduce new defined terms unless you also add their
  definition in the same redline.
- **Comments are for the counterparty.** Keep them short and neutral
  ("Cap aligned to 12 months of fees, consistent with the value at risk
  under this Agreement."). Internal reasoning, leverage and fallbacks
  belong in the memo, never in a margin comment.
- **Ask with a comment, not an edit,** when the right answer depends on
  facts you do not have.
- **Never delete a definition that is still used, and never change a
  section number** that other clauses point to. `references_resolve`
  checks both.
- **No silent changes.** Every difference between the original and the
  proposal must be a tracked change tied to an issue; `redline_roundtrip`
  re-applies the ops and compares both views of the .docx.

## Negotiation memo outline

```
> disclaimer

# Negotiation memo - <contract> (<side>)

## Summary
Overall risk, the three to five points that matter, escalations.

## Priorities
Ranked list: issue id, the ask, why it matters, walk-away if any.

## Fallbacks and concessions
Per issue: fallback positions in order, and low-value points that can be
traded.

## Open questions for counsel
Judgement calls, [review]-flagged items, hidden-content findings, missing
inputs.
```
