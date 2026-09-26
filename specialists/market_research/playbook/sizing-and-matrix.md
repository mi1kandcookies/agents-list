# Sizing model and competitor matrix

## Sizing

Build both methods; each input is `{name, value, claim}` or
`{name, value, assumption}` where `assumption` explains the number.

- A cited input uses the number exactly as its claim's quote states it:
  "22%" is 0.22, "$1.9 billion" is 1900000000, "91,200" is 91200. A number
  you derive (a midpoint, a conversion) is an assumption that names the
  claims it comes from. The top-down base and the bottom-up units and price
  must cite claims.
- **Top-down**: start from a published total (industry or category spend) and
  apply shares: `TAM = base x share_1 x share_2 ...`.
- **Bottom-up**: `TAM = units x price x factor_1 x factor_2 ...`, where units
  come from statistics (e.g. count of target businesses), price from dated
  pricing pages, and each factor is named: seats per unit, billing periods
  per year, addressable share. TAM assumes every addressable unit buys; do
  not multiply by current adoption here.
- **SAM** = TAM x serviceable share (geography, segment, channel, current
  adoption where it limits who can be served). **SOM** = SAM x obtainable
  share over the stated horizon. Shares are fractions between 0 and 1.
- If top-down and bottom-up TAM differ by more than 30% of the larger value,
  write a reconciliation note: which inputs drive the gap and which estimate
  the report leans on.
- Sensitivity: vary the two most uncertain inputs by -25%, -10%, +10%, +25%
  (labels such as `bottom_up.price` or `bottom_up.factors[0]`).

`build_sizing_model` computes all of this and writes
`deliverables/m3-report/market-sizing.json`; the acceptance check recomputes it,
so never edit the outputs by hand.

## Competitor matrix

- Rows are competitors; columns are dimensions agreed in the plan (target
  segment, pricing model, entry price, key features, deployment, funding,
  headcount ...).
- Every competitor the client named gets a row.
- Every filled cell is `value [C#]`. Empty cells mean "not found" and are
  listed in the report's open questions.
- Price columns and any cell stating a currency amount (prices, funding,
  revenue) also carry the date it was observed (`as of 2026-08-14`).
