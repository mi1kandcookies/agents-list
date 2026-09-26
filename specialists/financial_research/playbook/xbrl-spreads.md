# Spreads and comps from XBRL

## How the data is shaped

SEC companyfacts lists every value a company tagged in its filings: a tag
(e.g. `us-gaap:Revenues`), a unit (`USD`, `shares`, `USD/shares`), a period
(`start`..`end` for flows, `end` only for balances), the accession of the
filing that reported it, the form, and `fy`, the fiscal year of that filing.
A 10-K reports the current and the prior years, so the same period can
appear in several filings, sometimes with different values after a
restatement.

`build_spreads` resolves this deterministically:

- fiscal years come from the 10-Ks, not the calendar: each 10-K's own year
  is the latest full-year period it reports, labelled with the fiscal year
  the filer gave that 10-K (`fiscal_year_ends` in `edgar_companyfacts`
  output). A 52/53-week year ending on 1 January keeps its own label, and so
  does a retailer's year ending in January or February. Earlier years a 10-K
  shows only as comparatives take the labels below;
- flows must span 350-380 days and end on that fiscal year-end; balances
  must sit on it;
- among 10-K and 10-K/A facts for that exact period, the most recently filed
  wins: a restated comparative replaces the original figure, so every year
  and every growth rate is on the latest reported basis. Say so in the notes
  and list each restatement under Exceptions;
- tags are tried in a fixed order per metric (see `tools.METRICS`).

Each row of `facts.csv` keeps the tag, unit, period, form and accession, so a
reviewer can open the exact filing. Do not edit values. If you need a
different tag (a company reports revenue only under a less common us-gaap
tag), add the row from `xbrl_facts(tag=...)` output, which labels each fact
with its fiscal year, and explain it in `note`. Company-specific extension
tags are not in companyfacts; a figure only reported that way comes from
quoted filing text recorded in the ledger.

## Comps definitions (`compute_comps`)

| column | definition |
|---|---|
| total_debt | each borrowing once: long_term_debt_noncurrent + debt_current; else long_term_debt_noncurrent + long_term_debt_current + short_term_borrowings; else long_term_debt_total (which already includes the current portion) + short_term_borrowings; else the current items alone |
| ebitda | operating_income + d_and_a (reported D&A; no add-backs) |
| market_cap | price x shares_outstanding from the client's file, else x weighted-average diluted shares for the fiscal year |
| enterprise_value | market_cap + total_debt - cash + minority_interest + preferred |
| ev_revenue | enterprise_value / revenue |
| ev_ebitda | enterprise_value / ebitda; blank (n/m) when EBITDA <= 0 |
| pe | market_cap / net_income; blank (n/m) when earnings <= 0 |
| margins | gross_profit, operating_income, ebitda over revenue |
| revenue_growth | revenue / revenue_prior - 1 |

The debt components are separate metrics (`long_term_debt_noncurrent`,
`long_term_debt_current`, `long_term_debt_total`, `debt_current`,
`short_term_borrowings`), each one XBRL fact; `compute_comps` returns which
combination it used per company (`debt_basis`) for the Definitions section.

Price, price date, the current share count, minority interest and preferred
come only from the client's `inputs/market_data.csv`; comps never use the
indicative quotes `market_quote` fetches for the M4 pitch.
Numbers there may use thousands separators or a `$`. Without the file, or for
a company missing from it, the price and valuation columns stay blank and the
operating metrics are still delivered; say so in the notes. Multiples and
margins are rounded to four places; money is unrounded.

Weighted-average diluted shares only approximate the current share count.
If the client supplies `shares_outstanding`, it is used for market cap;
otherwise say in the notes that the weighted-average count was used.

## spreads_notes.md

Sections: **Method** (sources, fiscal years and their year-ends, selection
rule, latest-reported basis), **Definitions** (the table above, adapted, with
each company's debt basis), **Peer medians** (from `compute_comps`, with where
the target sits), **Exceptions** (every missing metric, note-carrying row,
restatement, non-December year end, n/m multiple, company without market
data).

## Acceptance you are held to

- `xbrl_tieout`: at least 98% of rows match a fact exactly (tag, unit,
  period, accession, value) and carry the fiscal year the 10-Ks give that
  period; every other row has a written note.
- `comps_tie_to_xbrl`: each sourced comps figure equals its facts.csv row,
  and that row is a real SEC fact; a column left blank fails when the SEC
  reports that metric for the year; market columns equal the client's file
  (blank when it has no row); every company in scope has a comps row.
- `comps_recompute`: every derived column recomputes from the row.
