# Red-flag checklist

Write `deliverables/m3-diligence-memo/red_flags.md` with one section per
company, headed `## <Company name> (CIK <number>)`, containing this table:

| ID | Item | Status | Evidence |
|---|---|---|---|

Status is one of `found`, `not_found`, `not_applicable`. Evidence for `found`
is an accession number (`0000000000-00-000000`), a ledger claim `[C#]`, or an
`inputs/` path. Evidence for `not_found` says what you reviewed.

| ID | Item | How to check |
|---|---|---|
| RF01 | Change in certifying accountant (8-K Item 4.01) | `filing_red_flags` |
| RF02 | Non-reliance on prior financial statements (8-K Item 4.02) | `filing_red_flags` |
| RF03 | Amended periodic reports (10-K/A, 10-Q/A) | `filing_red_flags`, then read why |
| RF04 | Late filing notices (NT 10-K, NT 10-Q) | `filing_red_flags` |
| RF05 | Going-concern doubt | search the latest 10-K audit report and liquidity notes |
| RF06 | Material weakness in internal control | Item 9A of the 10-K |
| RF07 | Related-party transactions | 10-K related-party note, proxy, data room |
| RF08 | Customer or supplier concentration | 10-K business and risk sections, data room |
| RF09 | Debt acceleration, default or covenant trigger (8-K Item 2.04) | `filing_red_flags`, debt note, credit agreement |
| RF10 | Material litigation or regulatory proceedings | Item 3 of the 10-K, contingencies note |

RF01-RF04 and RF09 are re-derived by the acceptance check from the SEC filing
index: if the index shows an event, the row must be `found` and cite that
accession. Accessions you cite must exist in the company's filing index, so
copy them from tool output.

For every `found` item, the memo's **Red flags** section explains what
happened, when, how it was resolved and what it implies, with citations.
Filing-level flags are prompts to read the filing, not conclusions: an
auditor change can be routine rotation, and a 10-K/A may only add Part III.
Say which it is once you have read it.

Defaults: look back three years from the latest filing unless the plan says
otherwise. An item you could not assess (text not available) is a question
for the client, listed in `questions.md`, and marked `not_found` with the
limitation stated, never silently skipped.
