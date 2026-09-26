# financial-research - Financial Research & Diligence Analyst

Public-company research and diligence for deal teams, corporate development,
lenders and small funds. The agent pulls filings and XBRL financials from SEC
EDGAR, indexes the client's data room, builds spreads and trading comps in
deterministic code, scans filing histories for red flags and writes a cited
memo. Every figure traces to an XBRL fact (tag, period, accession) or a
verbatim source quote, and the acceptance checks re-derive it from the source.

Not investment advice: the output is analysis for professional users. The
agent never issues ratings or price targets, never contacts management or
counterparties, and never trades or distributes work product.

## Milestones

| id | deliverables | automated acceptance |
|---|---|---|
| `m1-plan-sources` | `research_plan.md`, `source_inventory.csv`, `dataroom_index.csv` | sections, columns, `source_inventory_resolves`, `dataroom_index_complete`; rubric; client approves the peer set |
| `m2-spreads-comps` | `facts.csv` (long-form spreads), `comps.csv`, `spreads_notes.md` | `xbrl_tieout` (>= 98% exact, rest explained), `comps_tie_to_xbrl` (nothing the SEC reports left blank, every company in scope covered), `comps_recompute`; rubric |
| `m3-diligence-memo` | `memo.md`, `red_flags.md`, `questions.md` | `memo_figures_match` (figure tags match comps; `[C#]` quotes contain the figures), `no_recommendation_language`, `red_flag_checklist` (three-year window), disclaimer, `ledger_verified`, `citations_resolve` (memo, checklist, questions); rubric |

All deliverables live under `deliverables/<milestone-id>/`.

## Inputs

- Required intake: `companies` (tickers or CIKs), `research_question`,
  `sec_user_agent` (organization and contact email; SEC fair access requires a
  declared User-Agent, and the tools refuse to call EDGAR without one).
- Optional: `fiscal_years`, `inputs/market_data.csv` (`cik, ticker, price,
  price_as_of`, optionally `shares_outstanding, minority_interest, preferred`,
  from the client's licensed price source; needed for multiples, and without
  it M2 delivers operating comps with the valuation columns blank),
  `inputs/dataroom/` (data-room export), `audience`.
- Offline SEC snapshots can be supplied under `inputs/edgar/submissions/`
  (including older index pages), `inputs/edgar/companyfacts/`
  (`CIK##########.json`) and `inputs/edgar/company_tickers.json`; the tests
  and evals run this way.
- Companies the client names by CIK are passed to the coverage checks
  (`propose_milestones` sets `ciks` on `comps_tie_to_xbrl` and
  `red_flag_checklist`); tickers and names are resolved with
  `sec_company_lookup` during M1.

## Tools (`tools.py`)

`sec_company_lookup`, `edgar_submissions`, `edgar_companyfacts`,
`edgar_filing_text` (network via the kit's egress-checked fetch, cached under
`.agentkit/edgar/`), `xbrl_facts`, `build_spreads`, `compute_comps`,
`filing_red_flags`, `index_dataroom`. Fiscal years, the latest-reported
basis, the debt hierarchy and the comps definitions are in
`playbook/xbrl-spreads.md`.

## Checks (`checks.py`)

`xbrl_tieout`, `comps_tie_to_xbrl`, `comps_recompute`, `memo_figures_match`,
`no_recommendation_language`, `red_flag_checklist`,
`source_inventory_resolves`, `dataroom_index_complete`. They read the SEC JSON
cache (or `inputs/edgar/`), the market data file, the claim ledger and the
data-room files directly, and their fiscal-year, debt and multiple
arithmetic is written independently of the tools.

## Pricing

Engagements are billed per milestone through the SOW, typically 500 to 6,000
USDC (`listing.pricing`). `task_price_usdc` (5 USDC, a placeholder) is the
separate flat x402 price the operator stamps for one agent-to-agent task.

## Human gate

No licensed-reviewer gate is required for internal research
(`human_gate.required: false`); the client's deal lead approves the M1 peer set
and reviews the memo. If the work product goes to investors, LPs or the
public, or is used by a broker-dealer or registered adviser, an appropriately
licensed person must review it first; the disclaimer and the review checklist
in `agent.yaml` say so.

## Limits and policy

- Egress: `data.sec.gov` and `www.sec.gov` only. No market-data, news or web
  search access; prices and licensed data come from the client as files.
- No shell. 150 steps, 5M tokens, $60 and 4 hours per milestone run.
- XBRL coverage: US GAAP (`us-gaap`) filers with 10-K data. Foreign private
  issuers (IFRS, 20-F) and companies with non-standard tags need notes or
  ledger-quoted figures. Fiscal years follow each 10-K's own year (52/53-week
  and January year-ends keep the filer's labels), and every value is the
  latest 10-K figure for its period, so restatements flow through.
- Market cap uses the client's `shares_outstanding` when given, else
  weighted-average diluted shares for the fiscal year (an approximation).
- The red-flag window is at least three years back from the latest filing,
  stated in each checklist section. SEC's index JSON holds only recent
  filings; `edgar_submissions(since=...)` loads the older pages, and the
  check fails a window the loaded index does not cover.
- The data-room index marks readable only what `read_document` can read
  (text, Markdown, CSV, JSON, HTML, Word); PDFs, spreadsheets and decks are
  listed with a note asking for an export.

## Kit integration (`agent.py`)

`FinancialResearch` is a thin `Specialist` subclass; the kit wraps
`TOOL_DEFS` and `CHECK_DEFS` itself. The subclass sends the User-Agent the
client declared in intake `sec_user_agent` with every EDGAR request (the
model's `user_agent` argument is used only when the intake has none, e.g.
after an `ask_client` answer; `SEC_USER_AGENT` is the last resort), and
`validate_intake` flags a `sec_user_agent` without a contact email.

- Model-supplied paths in the tools go through the kit's `resolve_path`
  (inputs/ read-only, .agentkit/ refused, written files marked
  agent-authored); the checks resolve their paths with `jail_path`.
- `edgar_filing_text` registers the filing text as a ledger source
  (kind `tool`), so `record_claim` quotes from filings verify.
- `python -m agentkit validate financial-research` checks the wiring; the
  offline end-to-end runs are in
  `tests/specialists/test_financial_research_e2e.py`.
