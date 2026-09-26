# Sizing model and competitor matrix

## Sizing

Build both methods; each input is `{name, value, claim}` or
`{name, value, assumption}` where `assumption` explains the number.

- **Top-down**: start from a published total (industry or category spend) and
  apply shares: `TAM = base x share_1 x share_2 ...`.
- **Bottom-up**: `TAM = units x adoption x price`, where units come from
  statistics (e.g. count of target businesses), adoption from surveys or
  filings, and price from dated pricing pages.
- **SAM** = TAM x serviceable share (geography, segment, channel).
  **SOM** = SAM x obtainable share over the stated horizon.
- If top-down and bottom-up TAM differ by more than 30% of the larger value,
  write a reconciliation note: which inputs drive the gap and which estimate
  the report leans on.
- Sensitivity: vary the two most uncertain inputs by -25%, -10%, +10%, +25%.

`build_sizing_model` computes all of this and writes
`deliverables/m3-report/market-sizing.json`; the acceptance check recomputes it,
so never edit the outputs by hand.

## Competitor matrix

- Rows are competitors; columns are dimensions agreed in the plan (target
  segment, pricing model, entry price, key features, deployment, funding,
  headcount ...).
- Every filled cell is `value [C#]`. Empty cells mean "not found" and are
  listed in the report's open questions.
- Pricing cells also carry the date the price was observed (`as of
  2026-08-14`).
