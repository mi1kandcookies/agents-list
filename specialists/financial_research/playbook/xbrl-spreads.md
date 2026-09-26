# Spreads and comps from XBRL

## How the data is shaped

SEC companyfacts lists every value a company tagged in its filings: a tag
(e.g. `us-gaap:Revenues`), a unit (`USD`, `shares`, `USD/shares`), a period
(`start`..`end` for flows, `end` only for balances), the accession of the
filing that reported it, and the form. A 10-K reports the current and the
prior year, so the same period can appear in several filings, sometimes with
different values after a restatement.

`build_spreads` resolves this deterministically:

- fiscal year = calendar year of the period end;
- flows must span 350-380 days; balances must sit on the fiscal year-end date;
- among candidates, the value first reported in that year's own 10-K wins, then
  the most recently filed;
- tags are tried in a fixed order per metric (see `tools.METRICS`).

Each row of `facts.csv` keeps the tag, unit, period, form and accession, so a
reviewer can open the exact filing. Do not edit values. If you need a
different tag (a company reports revenue only under a custom or less common
tag), add the row from `xbrl_facts(tag=...)` output and explain it in `note`.
Restated comparatives are an exception worth a note, not a correction.

## Comps definitions (`compute_comps`)

| column | definition |
|---|---|
| total_debt | long_term_debt + short_term_debt |
| ebitda | operating_income + d_and_a (reported D&A; no add-backs) |
| market_cap | price x weighted-average diluted shares for the fiscal year |
| enterprise_value | market_cap + total_debt - cash + minority_interest + preferred |
| ev_revenue | enterprise_value / revenue |
| ev_ebitda | enterprise_value / ebitda; blank (n/m) when EBITDA <= 0 |
| pe | market_cap / net_income; blank (n/m) when earnings <= 0 |
| margins | gross_profit, operating_income, ebitda over revenue |
| revenue_growth | revenue / revenue_prior - 1 |

Price, price date, minority interest and preferred come only from the client's
`inputs/market_data.csv`. We do not fetch quotes. Multiples and margins are
rounded to four places; money is unrounded.

Using weighted-average diluted shares is an approximation of the current share
count. Say so in the notes, and if the client supplies a current share count,
explain any difference.

## spreads_notes.md

Sections: **Method** (sources, fiscal years, selection rule), **Definitions**
(the table above, adapted), **Peer medians** (from `compute_comps`, with where
the target sits), **Exceptions** (every missing metric, note-carrying row,
restatement, non-December year end, n/m multiple).

## Acceptance you are held to

- `xbrl_tieout`: at least 98% of rows match a fact exactly (tag, unit,
  period, accession, value); every other row has a written note.
- `comps_tie_to_xbrl`: each sourced comps figure equals its facts.csv row, and
  that row is a real SEC fact; market columns equal the client's file.
- `comps_recompute`: every derived column recomputes from the row.
