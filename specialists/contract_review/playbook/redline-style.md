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
- **No silent changes.** Every difference between the reviewed text and
  the proposal must come from an op that cites an issue; `redline_roundtrip`
  re-applies the ops and compares both views of the .docx. The check ties
  an op to its issue by id and section only: an op that edits a section
  other than the one its issue quotes needs a margin comment and is listed
  under `Cross-section edits` in redline.md, and whether it belongs there
  is the attorney's call.
- **Add no blanks.** A redline that inserts `[insert ...]`, `TBD` or
  similar is refused; ask with a comment instead. Blanks already in the
  counterparty's paper are theirs and may stay.

## Negotiation memo outline

```
# Negotiation memo - <contract> (<side>)

The disclaimer, word for word, as one plain paragraph (no ">" or list
marker in front of it).

## Summary
Overall risk, the three to five points that matter, escalations, and
which text was reviewed if the paper carried pending tracked changes.

## Priorities
Ranked list: issue id, the ask, why it matters, walk-away if any.

## Fallbacks and concessions
Per issue: fallback positions in order, and low-value points that can be
traded.

## Open questions for counsel
Judgement calls, [review]-flagged items, hidden-content findings, missing
inputs, cross-section edits, and each family marked absent or not
applicable that the playbook rates critical or high, by family id.
```
