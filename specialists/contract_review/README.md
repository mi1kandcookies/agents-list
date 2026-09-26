# Contract Review & Redline (`contract-review`)

A first-party specialist that reviews third-party commercial contracts
(NDAs, SaaS/MSAs, DPAs, vendor agreements, order forms; US and UK
commercial paper) against the customer's own negotiation playbook. It
produces draft legal work product for a **licensed attorney**: a codified
playbook, an issue list where every issue quotes the contract verbatim, and
surgical redlines as native Word tracked changes with a negotiation memo.

It never sends anything to a counterparty, never accepts, rejects or signs,
and never advises a non-lawyer. Every milestone ends in attorney sign-off.

## Milestones

| id | Deliverables | Automated checks |
|---|---|---|
| `m1-playbook` | `playbook.yaml` (machine-readable positions), `playbook.md` | `playbook_schema_valid` (at least 12 families, each with preferred / fallback / walk-away, no placeholders), `files_exist`, `no_placeholders`, `disclaimer_present` |
| `m2-issues` | `issues.json`, `issues.md`, `issues.csv`, `review-notes.md` | `quotes_in_contract`, `playbook_coverage`, `issue_list_valid`, `csv_formula_safe`, `hidden_content_disclosed`, `disclaimer_present` |
| `m3-redline` | `redline.docx`, `redline.json`, `redline.md`, `proposed.txt`, `memo.md` | `redline_roundtrip`, `references_resolve`, `memo_covers_issues`, `markdown_sections`, `no_placeholders`, `disclaimer_present` |

Each milestone also has a `rubric_grader` criterion (`rubrics/*.yaml`) and a
`human_signoff` criterion for the attorney.

What the checks guarantee, recomputed from source rather than taken from
the agent's report:

- every issue quote (and every "compliant" coverage quote) appears verbatim
  in the contract under `inputs/`, whose sha256 must match the recorded one;
- every family in the approved playbook is marked deviation, compliant,
  absent or not applicable, consistently with the issues raised, and every
  critical issue is escalated;
- the redline is re-applied from its ops: each target matches exactly once,
  `proposed.txt` equals the result, and in `redline.docx` "reject all"
  reproduces the original while "accept all" gives the proposal, so there
  are no silent edits;
- the redline breaks no cross-reference or used definition;
- CSV cells cannot be read as spreadsheet formulas;
- hidden text, comments, metadata, field codes, macros, external links and
  instructions aimed at an automated reviewer are disclosed to the attorney.

## Inputs (intake)

Required: the contract files (in `inputs/`, `.docx` preferred, `.txt`
accepted), contract type, the client's side (customer or vendor) and the
reviewing attorney. Optional: existing playbook or guidelines, templates,
previously negotiated agreements, governing jurisdiction, deal context and
the escalation contact.

## Tools

Kit tools: `read_document`, `read_file`, `write_file`, `edit_file`,
`list_files`, `search_files`, `ask_client`, `post_progress`,
`submit_milestone`.

Domain tools (`tools.py`, stdlib only, no network or subprocess):
`read_contract`, `segment_clauses`, `scan_hidden_content`, `locate_quote`,
`check_references`, `validate_playbook`, `record_issues`, `build_redline`.

## Human gate

`human_gate.required: true`, reviewer role **licensed attorney**. The
attorney approves the playbook before any review, every issue list and
redline before it leaves the workspace, any downgrade of a walk-away item,
and the negotiation memo. The disclaimer ("Draft work product prepared for
review by a licensed attorney. Not legal advice. ...") heads every Markdown
deliverable and is checked.

## Limits and scope

- No egress, no shell: everything runs on files in the workspace.
- `limits`: 80 steps, 3M tokens, USD 40, 180 minutes per milestone run.
- v1 reads `.docx`, `.txt` and `.md`. Scanned PDFs need a text version.
- The output `.docx` is a text-faithful redline: paragraphs and tracked
  changes are exact, but the original document's formatting, numbering
  styles and tables are not carried over. A formatting-preserving writer
  that edits the original package in place is future work.
- Quote matching tolerates only whitespace and straight versus curly
  quotes; clause segmentation relies on numbered headings.
- US and UK commercial contracts only. Other jurisdictions, regulated
  sectors and litigation are out of scope and routed to the attorney.
