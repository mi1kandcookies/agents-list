# Proposal & Questionnaire Writer (`proposal-writer`)

A first-party specialist that turns a solicitation into a compliant,
source-grounded response. It works for commercial RFPs, public-sector
tenders, small-business research proposals, foundation and state grants,
and security or vendor questionnaires (CSV).

Its two jobs are the ones that decide whether a bid counts: never miss a
requirement, and never say anything the customer's own documents do not
support. Every requirement is traced to the section that answers it, and
every company claim cites a knowledge-base passage that contains its numbers
and certifications. When the knowledge base is silent, the agent asks
instead of inventing.

## Milestones

| id | deliverables | automated acceptance |
|---|---|---|
| `m1-shred` | `requirements.json`, `compliance_matrix.csv`, `format_rules.json`, `bid-brief.md` | `shred_complete`, `matrix_consistent`, `format_rules_captured`, `dates_match_source`, builtins (files, JSON, CSV columns, sections, placeholders) |
| `m2-outline` | `evidence_map.csv`, `gaps.md`, `outline.md` | `evidence_map_complete`, `outline_budget_ok`, builtins |
| `m3-draft` | `proposal.md`, `compliance_matrix.csv`, `submission-checklist.md` (+ `questionnaire_answers.csv` in questionnaire mode) | `matrix_consistent` (all addressed), `draft_within_limits`, `claims_grounded`, `questionnaire_answers_grounded`, `ledger_verified`, builtins |

Each milestone also has a rubric check (`rubrics/*.yaml`, scored by the grader
model) and a human sign-off: the bid decision (m1), expert answers to the
gap questions (m2), and expert approval plus signature and submission by the
customer (m3).

All checks recompute from `inputs/`: the solicitation must be unchanged
since the shred (sha256), every requirement row must be verbatim from it,
matrix locations must match the markers in the draft, and cited passages
must contain the numbers and certifications stated.

## Inputs

- `inputs/solicitation/` - the solicitation, amendments and attachments as
  `.md`, `.txt` or `.docx` (convert PDFs to text first), or a questionnaire
  exported as `.csv`.
- `inputs/kb/` - the knowledge base: capability statement, past proposals,
  past-performance write-ups, resumes, policies, prior answers.
- Intake: engagement mode (`rfp` or `questionnaire`), deadline, funder
  AI-use policy, win themes, expert contacts.

## Tools

`shred_requirements`, `shred_questionnaire`, `extract_format_rules`,
`extract_dates`, `kb_search`, `build_evidence_map`, `check_page_budget`,
`update_compliance_matrix`, `grounding_report`, `init_answer_sheet`,
`set_answer` (see `tools.py`), plus the kit's document, workspace, ledger and
platform tools. No network egress and no shell.

## Human gate

The listing's human gate is not a licensed-reviewer gate, but the agent
never acts for the customer: the customer makes the bid decision, experts
confirm technical and past-performance claims, every price comes from the
customer, and the authorized representative signs every certification and
submits. The agent never logs into a portal.

## Limits

- 120 steps, 4M tokens, $50 of model spend and 4 hours per milestone run.
- Page counts in the draft are estimated from words (500 per page by
  default); the final layout check happens on the customer's formatted PDF.
- Keyword retrieval (BM25) proposes evidence; the agent must confirm each
  passage. Scanned PDFs, spreadsheets other than CSV, and classified,
  export-controlled or controlled unclassified material are out of scope.
- Research-grant narratives: structure, compliance and editing support only;
  the investigator owns the ideas, and the funder's AI-use policy is checked
  in the brief.

## Evals

`evals/fixtures/` holds synthetic workspaces (fictional organizations):
`transit-app` (RFP + knowledge base with grounding traps), `questionnaire`
(six questions, two of which must be abstained) and `injection` (hidden
instructions in the solicitation and the knowledge base). `evals/cases/*.json`
describe the runs and what a good result looks like.
