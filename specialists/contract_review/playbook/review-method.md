# Review method

## Milestone m1-playbook: codify the playbook

1. List what is in `inputs/`: guidelines, templates, negotiated agreements
   (ideally original and final versions), prior playbooks.
2. Confirm the contract type and the client's side (customer = buying,
   vendor = selling). Positions differ by side; do not mix them.
3. For each clause family in the checklist, derive the position from the
   client's materials in this order: written guidelines, then how past
   agreements were actually settled (compare original and final text), then
   the client's own template. Where nothing speaks to a family, write a
   baseline position labelled `baseline - attorney to confirm`.
4. Every family gets: `id`, `title`, `severity` (critical/high/medium/low
   when the position is missed), `preferred`, `fallback` (one or more,
   ordered), `walk_away`, and where useful `escalate_if` and `notes`.
5. Write `deliverables/m1-playbook/playbook.yaml`:

   ```yaml
   schema_version: 1
   contract_type: saas_msa
   side: customer
   jurisdiction_scope: US commercial
   escalation_contact: Deputy General Counsel
   families:
     - id: liability_cap_amount
       title: Limitation of liability - cap amount
       severity: high
       preferred: ...
       fallback: [...]
       walk_away: ...
       escalate_if: ...
   ```

6. Run `validate_playbook`. Then write `playbook.md`, a readable version
   for the attorney with a table per family and a "Sources" section saying
   where each position came from.

## Milestone m2-issues: review against the approved playbook

1. `scan_hidden_content` on the contract; note every finding. If it
   reports `tracked_changes`, settle the `base` (accepted or original)
   before reviewing and record it.
2. `read_contract` end to end; `segment_clauses`; `check_references`.
   Other files under `inputs/` (guidelines, templates, related documents)
   are context: only the intake's contract is reviewed.
3. Walk the playbook family by family. For each, find the governing text
   (it may be spread over definitions, the clause itself and exceptions
   elsewhere), compare it with preferred, fallback and walk-away, and
   decide: deviation, compliant, absent or not applicable.
4. For each deviation write an issue: id (`I1`, `I2`, ...), family,
   severity (at least the family's playbook severity, or `review_flag`
   plus a `rationale`), verbatim quote (the smallest span that proves the
   point), deviation, recommendation, fallback (required), rationale,
   `escalate`, `review_flag`.
5. For every other family write its coverage row: `compliant` with a
   quote from the clause that governs it; `absent` or `not_applicable`
   with a note saying why, plus `escalate` or `review_flag` when the
   playbook rates the family critical or high and `review_flag` when the
   contract's wording touches it. If the paper carries an instruction
   aimed at an automated reviewer and you raise no issue, every row gets
   `review_flag`.
6. Check interactions last: cap versus indemnity, carve-outs versus
   confidentiality and data breaches, renewal versus termination rights,
   assignment versus change of control.
7. `record_issues`. Fix whatever it rejects and call it again. Do not
   touch the approved playbook.
8. Write `review-notes.md`: disclaimer, scope and assumptions (including
   the base, if any), `## Hidden content` naming each kind of finding,
   escalations with the escalation contact, and questions for the
   attorney. No placeholders.

## Milestone m3-redline: redline and memo

1. Redline only approved issues. Build ops against the original text:
   `target_text` copied exactly (long enough to match once, inside one
   paragraph), `new_text` with the minimal change, a short counterparty-
   facing `comment` when the change needs a reason, and `issue_id`.
2. Use a comment-only op (no `new_text`) for points to raise rather than
   rewrite.
3. `build_redline` with `issues: deliverables/m2-issues/issues.json` (the
   default) on the same contract; the base comes from the issue list. Fix
   any rejected op. Check `unresolved_references_after` and
   `cross_section_edits`. Never re-record or edit the approved issue list.
4. Write `memo.md` for the attorney with the required headings, naming
   every critical, high and escalated issue by id and every critical or
   high family marked absent or not applicable by family id.
