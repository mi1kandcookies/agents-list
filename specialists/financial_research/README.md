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
| `m2-spreads-comps` | `facts.csv` (long-form spreads), `comps.csv`, `spreads_notes.md` | `xbrl_tieout` (>= 98% exact, rest explained), `comps_tie_to_xbrl`, `comps_recompute`; rubric |
| `m3-diligence-memo` | `memo.md`, `red_flags.md`, `questions.md` | `memo_figures_match`, `no_recommendation_language`, `red_flag_checklist`, disclaimer, `ledger_verified`, `citations_resolve`; rubric |

All deliverables live under `deliverables/<milestone-id>/`.

## Inputs

- Required intake: `companies` (tickers or CIKs), `research_question`,
  `sec_user_agent` (organization and contact email; SEC fair access requires a
  declared User-Agent, and the tools refuse to call EDGAR without one).
- Optional: `fiscal_years`, `inputs/market_data.csv` (`cik, ticker, price,
  price_as_of, minority_interest, preferred` from the client's licensed price
  source; needed for multiples), `inputs/dataroom/` (data-room export),
  `audience`.
- Offline SEC snapshots can be supplied under `inputs/edgar/submissions/` and
  `inputs/edgar/companyfacts/` (`CIK##########.json`); the tests and evals run
  this way.

## Tools (`tools.py`)

`edgar_submissions`, `edgar_companyfacts`, `edgar_filing_text` (network via
the kit's egress-checked fetch, cached under `.agentkit/edgar/`),
`xbrl_facts`, `build_spreads`, `compute_comps`, `filing_red_flags`,
`index_dataroom`. Comps definitions are in `playbook/xbrl-spreads.md`.

## Checks (`checks.py`)

`xbrl_tieout`, `comps_tie_to_xbrl`, `comps_recompute`, `memo_figures_match`,
`no_recommendation_language`, `red_flag_checklist`,
`source_inventory_resolves`, `dataroom_index_complete`. They read the SEC JSON
cache (or `inputs/edgar/`), the market data file and the data-room files
directly, and their arithmetic is written independently of the tools.

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
  ledger-quoted figures. Fiscal year = calendar year of the period end, so
  retailers with January year ends shift by one label; say so in the notes.
- Market cap uses weighted-average diluted shares for the fiscal year, an
  approximation of the current count.
- Only the filing index's `recent` block (about the last 1,000 filings) is
  scanned for red flags.

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
