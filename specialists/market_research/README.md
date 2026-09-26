# Market & Competitive Research specialist

Turns a research brief into a cited, reproducible market and competitor
report. Every claim in the evidence ledger carries a verbatim quote from an
archived snapshot of its source, and every figure in the competitor matrix,
the sizing model and the report cites a ledger claim. Acceptance re-runs the
verifier instead of trusting the agent's summary.

Profile: `research`. Human gate: none (customer acceptance per milestone).

## Milestones

| id | deliverables | key checks |
|---|---|---|
| `m1-plan` | `research-plan.md`, `questions.json` | sections, `question_tree_valid` (every leaf has >= 2 source types incl. a primary one), plan rubric, client approval |
| `m2-evidence` | `claims.csv`, `claim-map.json`, `contradictions.md` | `ledger_verified`, `evidence_export_matches_ledger`, `question_coverage`, `source_tier_mix` |
| `m3-report` | `report.md`, `competitor-matrix.csv`, `market-sizing.json` | `citations_resolve`, `matrix_cells_cited`, `sizing_model_consistent`, `report_answers_questions`, disclaimer, report rubric |

All deliverables live under `deliverables/<milestone-id>/`.

## Inputs (intake)

Required: `research_objective`, `market_definition`, `geography`.
Optional: `competitors`, `evidence_cutoff`, `allowed_domains` (narrows egress),
`tier1_share` (default 0.6), `client_documents` (placed in `inputs/`).

## Tools

Kit tools (workspace, `http_fetch`, `web_search`, ledger, `ask_client`,
`submit_milestone`) plus the deterministic domain tools in `tools.py`:

- `write_question_tree` - validates and saves the question tree
- `classify_source` - tiers a source (1 primary, 2 reputable secondary, 3 other);
  government, filing and community hosts are decided by the host, not by a hint
- `map_claims` - links verified claims to leaf questions, records
  insufficient-evidence notes
- `export_evidence` - re-verifies every quote and writes `claims.csv`
- `build_competitor_matrix` - cited cells, dated pricing cells
- `build_sizing_model` - top-down and bottom-up TAM, SAM, SOM, method gap and
  sensitivity table

The ledger is read from `.agentkit/ledger.json` with snapshots in
`.agentkit/sources/<id>.txt` (the kit's ledger layout).

## Human gate and limits

No licensed reviewer is required. The client approves the question tree,
source policy and evidence cutoff (m1) and accepts each milestone. The agent
never contacts third parties, never runs surveys or interviews and never uses
non-public information; primary research is proposed to the client and runs
from the client's own accounts. Reports carry the disclaimer "Not investment,
legal, tax or accounting advice"; output that feeds offering materials or
regulatory filings needs review by a qualified person.

Run limits: 120 steps, 4M tokens, $60, 240 minutes per milestone. Egress is
an allowlist (open web via the configured search provider by default; narrowed
to `allowed_domains` when given). No shell commands.

## Evals

`evals/fixtures/hvac-scheduling/` is a synthetic workspace (fictional
companies and figures) with a ledger, snapshots (one carrying a planted
injection) and an example question tree. `evals/cases/*.json` hold golden
cases for live-model evaluation; unit tests in
`tests/specialists/test_market_research_domain.py` run offline.
