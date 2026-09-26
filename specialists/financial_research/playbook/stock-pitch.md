# Stock pitch with a variant view (M4)

A stock is mispriced only against the expectations its price already holds.
So the pitch starts from the price, not from a target: turn the price into
the growth it pays for, form your own view from the filings, and pitch only
the gap, with the evidence for it and the facts that would prove it wrong.

M4 runs on its own; it needs no earlier milestone. If
`deliverables/m2-spreads-comps/` exists, reuse its facts.csv and comps.csv.

## First principles

- The price is a forecast. A reverse DCF turns it into the revenue CAGR the
  market is paying for under stated margin, reinvestment and discount
  assumptions. That number, not a consensus estimate, is what you argue with.
- A variant view is a specific, evidenced disagreement with that number:
  "the price pays for 9% a year; the filings support 12%, because ...".
  Less than two percentage points apart is not a view: the answer is pass.
- Evidence is the company's own reporting: XBRL history, segment revenue,
  backlog or remaining performance obligations, guidance, customer
  concentration, capacity and pricing language in the 10-K and 10-Q.
- Every view can be wrong. Name the observable facts, with thresholds, that
  would falsify it and when they would show.
- The direction follows the numbers: base-case upside above +10% is long,
  below -10% short, anything between is pass.

## Steps

1. **Company.** The one in intake `pitch_focus`, else the first of
   `companies`: `sec_company_lookup` (ticker to CIK), `edgar_submissions`,
   `edgar_companyfacts`. Peers are context only.
2. **History.** `xbrl_facts` for revenue, operating_income, d_and_a, capex
   and operating_cash_flow over four or five fiscal years; `reverse_dcf` also
   returns revenue, operating margin and revenue CAGR for them.
3. **Filings text.** `edgar_filing_text` on the latest 10-K (Item 1, Item 7:
   segments, backlog, outlook) and the latest 10-Q. `record_claim` each
   sentence you rely on, choosing sentences that state the figure you use.
4. **Price.** `market_quote(ticker)`: the client's `inputs/market_data.csv`
   when it lists the ticker, else an indicative, delayed public quote,
   registered as a source. `record_claim` its `price_text` so the pitch cites
   the price [C#] with its source and as-of time. market_snapshot.json must
   hold the pitch company's quote: quote a peer only with another `output`
   path. If no quote can be had, ask the client for `inputs/market_data.csv`
   and say so in the submission.
5. **What the market is pricing in.** `reverse_dcf` takes the latest fiscal
   year's revenue, diluted shares and net debt from XBRL (tag, period and
   accession recorded) and solves for the revenue CAGR over `years` at which
   the DCF equals the price. Set the assumptions from the company's record,
   never to reach a number:
   - operating_margin: steady-state EBIT margin, from the latest year and
     the recent range;
   - tax_rate: the effective rate in the 10-K tax note, else 0.21;
   - reinvestment_rate: share of after-tax operating income reinvested
     (capex - D&A + working capital), roughly growth / return on capital;
   - discount_rate: about 0.08-0.10 for a large US company, higher for a
     smaller, cyclical or levered one;
   - terminal_growth: 0.02-0.03, below the discount rate; years: 10.
   If no CAGR from -50% to 100% explains the price the tool writes nothing:
   revisit the margin or discount rate and say what the price would require.
6. **Your view.** The revenue CAGR you expect over the same years, argued
   from steps 2-3.
7. **Scenarios.** `scenario_valuation` with `bear`, `base` and `bull`. The
   base case uses your view's CAGR exactly; bear and bull move growth and
   margin (and the discount rate if the risk changes). Values must rank bear
   <= base <= bull. The tool returns the direction the base case sets.
8. **Write** `variant_view.json`, then `pitch.md`, then `submit_milestone`
   with all five deliverables. Never edit the tool-written files by hand;
   rerun the tool.

## variant_view.json

```json
{
  "ticker": "HLVI",
  "metric": "revenue_cagr",
  "horizon_years": 10,
  "market_implied": 0.0871,
  "our_view": 0.12,
  "delta": 0.0329,
  "direction": "long",
  "thesis": "The price pays for growth below the company's own record and backlog.",
  "evidence": [
    {"point": "Backlog grew 24% to $612.0 million, ahead of revenue", "claims": ["C2"]},
    "Management expects low double-digit revenue growth in 2025 [C3]",
    {"point": "Revenue grew 11.8% in FY2024 on analyzer shipments", "claims": ["C1"]}
  ],
  "falsifiers": ["Backlog below $500 million at any 2025 quarter-end",
                 "FY2025 revenue growth under 8% with stable pricing"],
  "catalysts": [{"event": "Q3 2025 results and backlog", "date": "2025-11-04"},
                {"event": "FY2025 10-K with the 2026 outlook", "date": "2026-02"}]
}
```

Rates are fractions (0.12 is 12%). `variant_view_grounded` requires:
`market_implied` = market_implied.json's `implied_revenue_cagr` (four
decimals is enough); `our_view` a revenue CAGR at least 0.02 away from it
and equal to the base scenario's `revenue_cagr`; `delta` = our_view -
market_implied; `direction` as the base-case upside sets it; three or more
distinct evidence items, each citing recorded claims ([C#] in the text or a
`claims` list); two or more specific falsifiers; two or more catalysts,
each saying when (a year, quarter or date).

## pitch.md

`# <Company> (<ticker>): <one-line thesis>`, the disclaimer verbatim, a line
`Direction: long` (or short, or pass), then these sections in order:

1. **Summary** - three to five sentences: thesis, what the price implies,
   your view, base-case value against the price, key catalyst, main risk.
2. **Thesis** - two to four numbered points, each with its evidence.
3. **What the market is pricing in** - price, source and as-of time [C#];
   the reverse DCF inputs (fiscal year, revenue, diluted shares, net debt,
   with accessions) and assumptions; the implied revenue CAGR as a
   percentage with one decimal (8.7%), set against the historical CAGR.
4. **Variant view** - your CAGR, the gap in points, and why, citing [C#].
5. **Catalysts** - dated, and which view each would confirm.
6. **Valuation** - the bear/base/bull table from valuation.csv (CAGR,
   margin, discount rate, value per share, upside) and what drives each.
   These are outputs of stated assumptions, not a price target.
7. **Risks and what would change our mind** - ranked risks and the
   falsifiers.
8. **Sources** - filings by accession, the quote source and time, the claims.

Write for a portfolio manager with five minutes: the answer first, short
paragraphs, no hype. Give no advice to any person to trade and do not call a
scenario value a price target; the client's investment professional adopts
or rejects the thesis.

## Acceptance you are held to

- `pitch_valuation_recompute`: the price ties to the client file or the
  registered quote; market_implied.json uses the latest fiscal year's XBRL
  revenue, diluted shares and every reported cash and debt component; the
  implied CAGR and every valuation row recompute.
- `variant_view_grounded`: the rules above; pitch.md states the same
  direction and the implied CAGR.
- `citations_resolve`, `ledger_verified`: every [C#] is a recorded claim
  that quotes its source verbatim.
