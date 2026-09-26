# Playbook: security and vendor questionnaires

Use this mode when intake `engagement_mode` is `questionnaire` (a CSV export
of a security, privacy or vendor-risk questionnaire).

## Milestones in questionnaire mode

- **m1-shred:** `shred_questionnaire` turns each question into a `Q-###`
  requirement (the original id is kept as `source_id`). Run
  `extract_format_rules` on the file too (usually no rules). The brief covers
  scope, question count by category, the due date from intake, questions
  likely to need an expert, and a recommendation on answer owners.
- **m2-outline:** `build_evidence_map` over the knowledge base; gaps become
  expert questions in `gaps.md`. The outline groups questions by category
  with the owner for each group.
- **m3-draft:** `init_answer_sheet`, then one `set_answer` per question.
  The sheet is `deliverables/m3-draft/questionnaire_answers.csv`; include it
  in the submitted artifacts. `proposal.md` is a short cover response
  (scope, answered vs needs-review counts, what the customer must confirm)
  with its own citations.

## Grounded or abstain

- `answered` requires at least one KB citation, and every number and
  certification in the answer must appear in the cited passage.
  `set_answer` refuses anything else.
- When the knowledge base does not establish the fact, the answer is
  `needs_review` with the open question: "NEEDS REVIEW: no SOC 2 report in
  the knowledge base; confirm current attestation status."
- Never answer "Yes" to a control, certification or attestation because it
  is common practice. A confident wrong answer is worse than an abstention:
  questionnaire answers often become contractual representations.
- Keep answers short and literal: yes/no first when the question is yes/no,
  then the evidence in one or two sentences.
- Say what the policy states, not how well it operates; operating
  effectiveness is an auditor's judgment.

## Reuse

Prior accepted answers in the knowledge base are good evidence when they are
current; prefer the policy or report they point to when both exist, and flag
answers older than a year in the cover response.
