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
| `m2-issues` | `issues.json`, `issues.md`, `issues.csv`, `review-notes.md` | `approved_inputs_unchanged`, `quotes_in_contract`, `playbook_coverage`, `issue_list_valid`, `csv_formula_safe`, `hidden_content_disclosed`, `no_placeholders`, `disclaimer_present` |
| `m3-redline` | `redline.docx`, `redline.json`, `redline.md`, `proposed.txt`, `memo.md` | `approved_inputs_unchanged`, `redline_roundtrip`, `references_resolve`, `memo_covers_issues`, `markdown_sections`, `no_placeholders`, `disclaimer_present` |

Each milestone also has a `rubric_grader` criterion (`rubrics/*.yaml`) and a
`human_signoff` criterion for the attorney.

What the checks verify, recomputed from source rather than taken from the
agent's report:

- the reviewed contract is the one the intake names (the harness pins it
  under `.agentkit/` before each run, where the agent cannot write), is a
  file under `inputs/` (judged on the resolved path, so a file the agent
  wrote cannot stand in for it), and still has the recorded sha256;
- the approved playbook (m2, m3) and the approved issue list and review
  notes (m3) are byte-for-byte what was approved: the artifact hashes in
  the earlier milestone's submission, else the hashes the harness recorded
  before the run (kept across resumes, so an interrupted run cannot
  re-approve its own edit);
- every issue quote and every "compliant" coverage quote appears verbatim
  (whitespace and quote style aside) in the reviewed text;
- every family in the approved playbook is marked deviation, compliant,
  absent or not applicable, consistently with the issues raised; critical
  issues are escalated and every issue has a fallback;
- where a check cannot grader the legal call itself, it makes the agent put
  the call in front of the attorney as `[review]` in `issues.md`: an issue
  rated below its family's playbook severity (with a rationale), a
  compliant quote reused for another family or not in a clause about the
  family (keyword hints for the default families), an absent or
  not-applicable family the playbook rates critical or high or that the
  contract's wording points to, and every row when the paper carries an
  instruction aimed at an automated reviewer and nothing was raised. The
  memo must name the critical and high families left absent or not
  applicable. These rules make a gutted or instruction-following review
  visible; they do not replace the attorney's reading;
- `issues.md` (with every field, flag and the disclaimer) and `issues.csv`
  are exactly what `record_issues` renders from `issues.json`, so the
  attorney's view cannot drift from the checked record;
- the redline is re-applied from its ops to the same contract, hash and
  base as the issue list: each target matches exactly once, every op cites
  a recorded issue, `proposed.txt` equals the result, and in
  `redline.docx` "reject all" reproduces the reviewed text while "accept
  all" gives the proposal, so there are no silent edits; its margin
  comments are exactly the ops' comments and `redline.md` is the rendering
  of the ops. An op is tied to its issue by id and top-level section: one
  that edits another section must carry a comment and is listed under
  "Cross-section edits" in `redline.md`. The redline may not add
  placeholders. The manifest pins each delivered path, so `redline.json`
  cannot point a check at another file;
- the redline breaks no cross-reference or used definition;
- CSV cells cannot be read as spreadsheet formulas;
- hidden text, comments, pending tracked changes, metadata, field codes,
  macros and external links are disclosed under a `Hidden content`
  heading in the review notes, by kind. Instructions aimed at an automated
  reviewer are found by a phrase heuristic that catches common wordings,
  not every one; the defence that does not depend on it is that tool
  output reaches the model as untrusted data and the attorney reviews
  everything.

## Inputs (intake)

Required: the contract file (in `inputs/`, `.docx` preferred, `.txt`
accepted), contract type, the client's side (customer or vendor) and the
reviewing attorney. Optional: related documents the contract incorporates
(order forms, a DPA, policies), existing playbook or guidelines,
templates, previously negotiated agreements, governing jurisdiction, deal
context and the escalation contact.

`agent.py` (`ContractReview`) adds three blocking intake rules: the side
must name exactly one of customer or vendor; the contract file must be
`.docx`, `.txt` or `.md`; and each engagement reviews one contract. A
bundle (MSA + DPA + order form) is one engagement per document to be
redlined, with the others listed under `related_documents` and read for
context. The estimate is per contract. A sample intake lives in
`evals/fixtures/northwind_intake.json`.

## Tools

Kit tools: `read_document`, `read_file`, `write_file`, `edit_file`,
`list_files`, `search_files`, `ask_client`, `post_progress`,
`submit_milestone`.

Domain tools (`tools.py`, stdlib and PyYAML only, no network or
subprocess): `read_contract`, `segment_clauses`, `scan_hidden_content`,
`locate_quote`, `check_references`, `validate_playbook`, `record_issues`,
`build_redline`. They resolve paths through the kit's policy (no
`.agentkit/`, `inputs/` read-only) and every file they write is recorded as
agent-authored. Domain checks are registered as `automated`, so no brief can
turn one into a pending check.

## Running

```bash
python -m agentkit validate contract-review
python -m agentkit estimate contract-review --intake specialists/contract_review/evals/fixtures/northwind_intake.json
python -m agentkit spec-hash contract-review
python -m agentkit run contract-review --brief brief.json --milestone m1-playbook --milestone-idx 0 \
    --workspace WORKDIR --spec-hash 0x...
python -m agentkit check contract-review --milestone m1-playbook --workspace WORKDIR
```

`--milestone-idx` is the milestone's SOW index, which the evidence names;
`--spec-hash` is the value from the operator-stamped manifest, and the run
refuses a package that hashes differently. Runs use the models in
`agent.yaml` (no fallbacks), so production runs the stamped model.

Milestones run in order on one workspace: m2 reviews against the approved
`deliverables/m1-playbook/playbook.yaml`, m3 redlines the issues in
`deliverables/m2-issues/issues.json`. `approved_inputs_unchanged` compares
those files with the earlier milestone's submission; after the attorney
edits one outside the harness, resubmit that milestone so the edit becomes
the approved version. The m3 eval case starts from
`evals/fixtures/m3_msa_workspace` (an issue list written by
`record_issues`); fixture bytes are pinned by `evals/fixtures/.gitattributes`
because the records hash them. Offline end-to-end runs with a scripted
model are in `tests/specialists/test_contract_review_e2e.py`.

## Human gate

`human_gate.required: true`, reviewer role **licensed attorney**. The
attorney approves the playbook before any review, every issue list and
redline before it leaves the workspace, any downgrade of a walk-away item,
and the negotiation memo. The disclaimer ("Draft work product prepared for
review by a licensed attorney. Not legal advice. ...") heads every Markdown
deliverable and is checked.

## Limits and scope

- No egress, no shell: everything runs on files in the workspace.
- `limits`: 80 steps, 3M tokens, USD 40, 180 minutes per milestone run;
  each milestone is estimated at up to 3 agent-hours to match.
- v1 reads `.docx`, `.txt` and `.md`. Scanned PDFs need a text version.
  Text files may be UTF-8, UTF-16 (with a BOM) or Windows-1252; form
  feeds (PDF page breaks) and other line breaks start a new paragraph.
- A `.docx` with the counterparty's pending tracked changes is reviewed
  and redlined on one declared `base`: their changes accepted, or
  rejected. The output redline carries only our changes on that base,
  not theirs; paragraph-level revisions (split or merged paragraphs) are
  not modelled.
- The output `.docx` is a text-faithful redline: paragraphs and tracked
  changes are exact, but the original document's formatting, numbering
  styles and tables are not carried over. A formatting-preserving writer
  that edits the original package in place is future work.
- Quote matching tolerates only whitespace, invisible characters and
  straight versus curly quotes; redline targets match the same way and
  keep the contract's own characters. Clause segmentation and the family
  keyword hints rely on numbered headings and simple keywords.
- US and UK commercial contracts only. Other jurisdictions, regulated
  sectors and litigation are out of scope and routed to the attorney.
