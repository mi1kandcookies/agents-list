# Memo style

`deliverables/m3-diligence-memo/memo.md` is an investment-committee-style
research memo for a professional reader. Sections, in order:

1. **Summary** - answer the approved key questions in five to eight sentences
   with the supporting figures. The disclaimer sits directly under the title,
   starting "Not investment advice."
2. **Business overview** - what the company sells, to whom, how it makes money,
   segments, cited to the 10-K or data room.
3. **Financial profile** - growth, margins, cash conversion, leverage over the
   spread years, from comps and spreads.
4. **Peer comparison** - where the target sits against peer medians on each
   multiple and margin, and why (mix, scale, cycle), with the caveats from
   `spreads_notes.md`.
5. **Bull case** and 6. **Bear case** - each argued at full strength with
   evidence. The bear case is not a list of generic risks.
7. **Key risks** - ranked by likelihood and impact, each with what would
   confirm or refute it.
8. **Red flags** - narrative for each `found` checklist item.
9. **Open questions** - the most important gaps; full list in `questions.md`.
10. **Sources** - the filings and documents relied on (accession or path).

## Writing rules

- Lead with the answer. Short paragraphs, plain words, no hype.
- Every figure carries `[F:<cik>:<fy>:<column>]` or `[C#]`. Tables too: tag each
  cell or the row.
- Show precision you can defend: money in $ millions with one decimal,
  percentages with one decimal, multiples with one or two decimals.
- Separate fact from interpretation ("Revenue grew 4.5% [F:...]" vs "which
  suggests pricing held"). Mark interpretation as yours.
- No recommendation language: no ratings, price targets, "attractive entry
  point", "we recommend buying". Say what the evidence shows; the client
  decides.

## questions.md

A prioritized list for management or expert calls (the client runs them; the
agent never contacts anyone). Each question: priority (high / medium / low),
the question, and the memo section or source gap that motivates it, e.g.
"(Red flags, RF01) What prompted the June 2024 auditor change, and were there
disagreements?" Add a second list of data-room gaps: documents that are missing
and the risk each would resolve.
