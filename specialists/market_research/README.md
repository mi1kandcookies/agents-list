# Market & Competitive Research specialist

Turns a research brief into a cited, reproducible market and competitor
report. Every claim in the evidence ledger carries a verbatim quote from an
archived snapshot of its source; every competitor-matrix cell cites a ledger
claim; every sizing input either states the figure its claim quotes or is
written down as an assumption. Acceptance re-runs the verifier instead of
trusting the agent's summary.

Profile: `research`. Human gate: none (customer acceptance per milestone).

## Milestones

| id | deliverables | key checks |
|---|---|---|
| `m1-plan` | `research-plan.md`, `questions.json` | sections, `question_tree_valid` (every leaf has >= 2 source types incl. a primary one; the source map names the sites to fetch), plan rubric, client approval |
| `m2-evidence` | `claims.csv`, `claim-map.json`, `contradictions.md` | `ledger_verified` (>= 5 claims), `evidence_export_matches_ledger`, `question_coverage` (2 independent sources per leaf or an "insufficient evidence: ..." note, at most half noted), `source_tier_mix` (per distinct external source), `claims_not_from_instructions` (and the log names flagged sources) |
| `m3-report` | `report.md`, `competitor-matrix.csv`, `market-sizing.json` | `citations_resolve`, `claims_not_from_instructions`, `matrix_cells_cited`, `matrix_covers_competitors`, `sizing_model_consistent`, `report_answers_questions` (at most half unresolved), disclaimer, report rubric |

All deliverables live under `deliverables/<milestone-id>/`. From m2 on, the
tools and checks read `questions.json` as submitted in m1-plan (its hash is
in the m1 submission); a plan edited afterwards fails the checks and opens no
new sites.

## Inputs (intake)

Required: `research_objective`, `market_definition`, `geography`.
Optional: `competitors` (each must get a filled matrix row), `evidence_cutoff`
(facts published or valid after it are out of scope; the export flags facts
older than 18 months before it), `allowed_domains` (host rules; when given,
they replace the plan's source map as the sites the agent may fetch),
`tier1_share` (a share in (0, 1] that replaces the 0.6 threshold of
`source_tier_mix`), `client_documents` (placed in `inputs/`).
`validate-intake` reports a malformed `allowed_domains` or `tier1_share` as
blocking.

## Tools

Kit tools (workspace, `http_fetch`, ledger, `ask_client`, `submit_milestone`)
plus the deterministic domain tools in `tools.py`:

- `write_question_tree` - validates and saves the question tree and the
  source map (`source_domains`: site host rule -> source type)
- `classify_source` - tiers a source (1 primary, 2 reputable secondary, 3
  other). Government, filing and community hosts are decided by the host;
  a company or standard page is tier 1 only when the approved source map
  lists its site with that type, so a declared type alone never reaches tier 1
- `map_claims` - links verified claims to leaf questions, records
  insufficient-evidence notes and each fact's as-of date
- `export_evidence` - re-verifies every quote, writes `claims.csv`, and
  reports planted, unmapped, undated, stale and after-cutoff claims and
  sources whose text gives instructions
- `build_competitor_matrix` - cited cells; prices and currency amounts dated
- `build_sizing_model` - top-down (base x shares) and bottom-up (units x
  price x named factors) TAM, SAM, SOM, method gap and sensitivity table.
  Shares are fractions in [0, 1]; base, units and price must cite a claim
  whose quote states that number

The claim ledger is the kit's (`.agentkit/ledger.json`, snapshots in
`.agentkit/sources/<id>.txt`), and the domain tools and checks verify claims
with the kit's own rules. On top of them, a claim whose quote lies inside
text that addresses the agent ("ignore previous instructions and report
that ...", with the sentences around it) is refused by the tools and fails
`claims_not_from_instructions`; this is a pattern heuristic, not a proof.

`agent.py` holds the `Specialist` subclass: it registers the domain checks as
`automated` (a brief cannot turn one into a pending check), applies the
intake's `tier1_share` and `competitors`, and adds the source-plan egress
rule below.

## Egress

There is no web search (the kit has no search provider wired into a run);
the agent fetches known URLs with `http_fetch`, public hosts only.

- m1-plan fetches nothing: the plan is built from the brief and `inputs/`.
- m2 and m3 fetch only from the client's `allowed_domains` when the intake
  gives them, otherwise from the `source_domains` of the plan the client
  approved in m1-plan. The rule is read before the model's first step, so
  a plan rewritten mid-run never widens access.

Client files and untrusted pages share the model's context, so this bounds
where client material could be sent to sites the client accepted. It does not
stop a page on an approved site from asking the agent to put data into a URL
on that same site; the OS-level egress controls of the runtime are the
backstop.

## Human gate, limits and estimate

No licensed reviewer is required. The client approves the question tree,
source map and evidence cutoff (m1) and accepts each milestone. The agent
never contacts third parties, never runs surveys or interviews and never uses
non-public information; primary research is proposed to the client and runs
from the client's own accounts. Reports carry the disclaimer "Not investment,
legal, tax or accounting advice"; output that feeds offering materials or
regulatory filings needs review by a qualified person.

Run limits: 120 steps, 4M tokens, $60, 240 minutes per milestone. The
estimate (1-2, 2-4 and 2-4 agent hours) stays within the wall-clock limit,
and its $15 per agent-hour is the $60 budget spread over 4 hours. No shell
commands.

## Evals

`evals/fixtures/hvac-scheduling/` is a synthetic workspace (fictional
companies and figures) with a ledger, snapshots (S5 carries a planted
injection), an approved plan with a source map, and a client brief.
`evals/fixtures/hvac-scheduling-m2/` holds the m2 deliverables the m3 case
starts from. `evals/cases/*.json` hold golden cases for live-model evaluation
(`python -m agentkit eval market-research`); each declares the fixture files
its milestone starts from. The fixture's hosts are fictional, so the m2 case
puts the planted page in `inputs/` where the model can read it. Offline
tests: `tests/specialists/test_market_research_domain.py` (tools and checks)
and `tests/specialists/test_market_research_e2e.py` (all three milestones
through the kit with a scripted model, egress, and the negative paths).
