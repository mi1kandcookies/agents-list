# financial-research - Financial Research & Diligence Analyst

Public-company research and diligence for deal teams, corporate development,
lenders and small funds. The agent pulls filings and XBRL financials from SEC
EDGAR, indexes the client's data room, builds spreads and trading comps in
deterministic code, scans filing histories for red flags, writes a cited
memo and, on its own, a stock pitch with a variant view. Every figure traces
to an XBRL fact (tag, period, accession), a recorded price quote or a
verbatim source quote, and the acceptance checks re-derive it from the source.

Not investment advice: the output is analysis for professional users. The
memo never carries ratings or price targets; the stock pitch states a thesis
direction and bear/base/bull scenario values that the client's investment
professional adopts or rejects. The agent never contacts management or
counterparties, and never trades or distributes work product.

## Milestones

| id | deliverables | automated acceptance |
|---|---|---|
| `m1-plan-sources` | `research_plan.md`, `source_inventory.csv`, `dataroom_index.csv` | sections, columns, `source_inventory_resolves`, `dataroom_index_complete`; rubric; client approves the peer set |
| `m2-spreads-comps` | `facts.csv` (long-form spreads), `comps.csv`, `spreads_notes.md` | `xbrl_tieout` (>= 98% exact, rest explained), `comps_tie_to_xbrl` (nothing the SEC reports left blank, every company in scope covered), `comps_recompute`; rubric |
| `m3-diligence-memo` | `memo.md`, `red_flags.md`, `questions.md` | `memo_figures_match` (figure tags match comps; `[C#]` quotes contain the figures), `no_recommendation_language`, `red_flag_checklist` (three-year window), disclaimer, `ledger_verified`, `citations_resolve` (memo, checklist, questions); rubric |
| `m4-stock-pitch` | `pitch.md`, `market_snapshot.json`, `market_implied.json`, `valuation.csv`, `variant_view.json` | sections, disclaimer, `pitch_valuation_recompute` (price ties to its source, reverse DCF inputs to XBRL facts, every value recomputes), `variant_view_grounded` (2+ points from what is priced in, cited, falsifiable, dated), `ledger_verified`, `citations_resolve`; rubric; the client's investment professional signs off |

All deliverables live under `deliverables/<milestone-id>/`. M1-M3 build on
each other; M4 runs on its own.

## Inputs

- Required intake: `companies` (tickers or CIKs), `research_question`,
  `sec_user_agent` (organization and contact email; SEC fair access requires a
  declared User-Agent, and the tools refuse to call EDGAR without one).
- Optional: `fiscal_years`, `inputs/market_data.csv` (`cik, ticker, price,
  price_as_of`, optionally `shares_outstanding, minority_interest, preferred`,
  from the client's licensed price source; needed for multiples, and without
  it M2 delivers operating comps with the valuation columns blank; for M4 it
  takes precedence over the fetched quote), `inputs/dataroom/` (data-room
  export), `audience`, `pitch_focus` (the pitch company and any view to test).
- M4 needs no uploads: tickers, the question and the SEC contact are enough.
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
`filing_red_flags`, `index_dataroom`, and for the pitch `market_quote`,
`reverse_dcf`, `scenario_valuation`. Fiscal years, the latest-reported
basis, the debt hierarchy and the comps definitions are in
`playbook/xbrl-spreads.md`; the pitch method is in `playbook/stock-pitch.md`.

## Checks (`checks.py`)

`xbrl_tieout`, `comps_tie_to_xbrl`, `comps_recompute`, `memo_figures_match`,
`no_recommendation_language`, `red_flag_checklist`,
`source_inventory_resolves`, `dataroom_index_complete`,
`pitch_valuation_recompute`, `variant_view_grounded`. They read the SEC JSON
cache (or `inputs/edgar/`), the market data file, the claim ledger and the
data-room files directly, and their fiscal-year, debt and multiple
arithmetic is written independently of the tools. The one shared piece is
the pitch's DCF formula, `tools.dcf_value_per_share`.

## Stock pitch with a variant view (M4)

A stock is mispriced only against the expectations its price already holds,
so the pitch starts from the price. With no paid consensus data, the free
proxy for what the market expects is a reverse DCF:

1. `market_quote` takes the price from `inputs/market_data.csv` when it lists
   the ticker, else an indicative, delayed public quote (Yahoo's chart
   endpoint, then Nasdaq's), saves `market_snapshot.json` and registers the
   raw response as a ledger source, so the pitch cites the price as `[C#]`.
2. `reverse_dcf` takes the latest fiscal year's XBRL revenue, diluted
   weighted-average shares and net debt (every reported debt component less
   cash), each with tag, period and accession, and solves by bisection for
   the revenue CAGR in [-50%, 100%] at which the DCF equals the price (to
   0.01%), under stated margin, tax, reinvestment, discount-rate,
   terminal-growth and horizon assumptions: `market_implied.json`.
3. The agent forms its own view from the filings (growth and margin history
   from XBRL, segment, backlog and guidance language quoted from 10-K and
   10-Q text) and writes `variant_view.json`: its revenue CAGR at least two
   points away from the implied one, three or more cited pieces of evidence,
   falsifiers and dated catalysts.
4. `scenario_valuation` values bear, base (the agent's view) and bull per
   share against the price: `valuation.csv`. Base-case upside above +10% is
   long, below -10% short, else pass.
5. `pitch.md`: Summary, Thesis, What the market is pricing in, Variant view,
   Catalysts, Valuation, Risks and what would change our mind, Sources.

The model: free cash flow each year = revenue x operating margin x
(1 - tax rate) x (1 - reinvestment rate), discounted over the explicit years,
plus a Gordon terminal value; equity = enterprise value - net debt. Net debt
counts cash and cash equivalents only (not marketable securities); minority
interest, preferred stock and leases are not deducted, and diluted shares
are the fiscal year's weighted average (the tool warns when the latest cover
page count differs by more than 20%, e.g. after a split). The memo's
`no_recommendation_language` check does not apply to the pitch; the
disclaimer does, and the human sign-off belongs to the client's investment
professional.

## Run it on a VM

The M4 pitch needs no uploads: tickers, a research question and an SEC
contact are enough. On a machine with Python 3.12 (and its venv module), git
and outbound HTTPS to SEC EDGAR, the three quote hosts above and the model
provider:

```bash
git clone https://github.com/mi1kandcookies/agents-list.git && cd agents-list
git checkout feat/65-claude-specialist-financial-research
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-agents.txt
export ANTHROPIC_API_KEY=...                           # runs the manifest's Anthropic models
export SEC_USER_AGENT="Your Org you@yourdomain.com"    # SEC fair access: a name and an email
```

Edit `specialists/financial_research/examples/stock-pitch-brief.json`:
`companies`, `research_question`, `pitch_focus`, and `sec_user_agent`, which
SEC sees ahead of `SEC_USER_AGENT`, so replace its placeholder with your
organization and email. Then, from the repo root:

```bash
python -m agentkit validate financial-research    # prints [] when the wiring is sound
python -m agentkit run financial-research \
  --brief specialists/financial_research/examples/stock-pitch-brief.json \
  --milestone m4-stock-pitch --workspace ../pitch-ws > ../pitch-run.jsonl; echo "exit $?"
```

- The workspace must be outside the repo (the harness refuses a working
  directory inside it) and is created if missing. Use a fresh one per pitch;
  `--resume` continues a run that stopped early.
- stdout carries JSON-line events (tool calls, egress, check results). The
  deliverables land in `../pitch-ws/deliverables/m4-stock-pitch/`: `pitch.md`,
  `market_snapshot.json`, `market_implied.json`, `valuation.csv`,
  `variant_view.json`. The submission, with every check result and the
  evidence hash, is `../pitch-ws/.agentkit/submissions/m4-stock-pitch.json`;
  SEC JSON is cached under `.agentkit/edgar/` and quoted sources under
  `.agentkit/sources/`.
- Exit codes: `0` ready_for_review (every automated check passed; the rubric
  score and the human sign-off may still be pending), `3` a submission was
  written but is not ready (needs_revision, incomplete or budget_exceeded:
  read `check_results` in the submission), `2` usage or configuration error,
  nothing ran.
- Spend: a run stops at the manifest's `limits`: `max_usd: 60`, 150 steps,
  5M tokens, 240 minutes. The M4 estimate is 2-4 agent-hours at $8 an hour.
  The grader model (`models.grader`) scores the rubric; `--no-grader` leaves
  it pending.

Re-run the checks on a workspace without the model (`--grader` also scores
the rubric), or use another provider in development (without the first
variable the overrides are ignored with a warning and the manifest's models
run):

```bash
python -m agentkit check financial-research --milestone m4-stock-pitch --workspace ../pitch-ws
export AGENTKIT_ALLOW_MODEL_OVERRIDE=1 AGENTKIT_MODEL=openai:<model> OPENAI_API_KEY=...
export AGENTKIT_GRADER_MODEL=openai:<model>          # or run with --no-grader
```

Troubleshooting:

- SEC answers 403: the User-Agent lacks a real organization name and contact
  email (the brief's `sec_user_agent` first, then `SEC_USER_AGENT`); SEC also
  throttles clients above 10 requests a second.
- "no quote for ...": the quote hosts are blocked or rate-limiting the VM.
  In a fresh workspace, create `inputs/market_data.csv` with the header
  `cik,ticker,price,price_as_of` and a row per ticker from a licensed source,
  then run again; the client file always wins over a fetched quote.
- Exit 2 with "working directory ... inside the workspace": run from the
  repo root with a workspace outside it.

## Pricing

Engagements are billed per milestone through the SOW, typically 500 to 6,000
USDC (`listing.pricing`). `task_price_usdc` (5 USDC, a placeholder) is the
separate flat x402 price the operator stamps for one agent-to-agent task.

## Human gate

No licensed-reviewer gate is required for internal research
(`human_gate.required: false`); the client's deal lead approves the M1 peer set
and reviews the memo, and the client's investment professional reviews the M4
thesis direction and scenarios before any use. If the work product goes to
investors, LPs or the public, or is used by a broker-dealer or registered
adviser, an appropriately licensed person must review it first; the
disclaimer and the review checklist in `agent.yaml` say so.

## Limits and policy

- Egress: SEC EDGAR (`data.sec.gov`, `www.sec.gov`) plus indicative public
  quotes for the pitch (`query1.finance.yahoo.com`,
  `query2.finance.yahoo.com`, `api.nasdaq.com`). No news or web search
  access. Licensed prices still come from the client as
  `inputs/market_data.csv` and take precedence; comps use only that file.
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
