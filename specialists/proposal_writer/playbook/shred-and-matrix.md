# Playbook: requirement shred, compliance matrix and bid brief (m1-shred)

## What counts as a requirement

A requirement is any statement the evaluator can hold the response to:

- **Binding language:** shall, must, is/are required to, required,
  mandatory, is limited to, may not exceed, not to exceed. "Shall not" is a
  prohibition and is still a requirement.
- **"Will"** binds when the offeror, proposal or response is the subject
  ("Offerors will describe..."). "The District will post answers" is
  information, not a requirement. "Proposals will be evaluated on..." is an
  evaluation criterion and belongs in the matrix as type `evaluation`.
- **Implicit requirements:** required-forms tables, evaluation factors
  written as headings, attachment lists. Add them as extra rows with the
  exact source sentence (the check verifies every row is verbatim).
- **Not requirements:** "should" and "may" statements are preferences; note
  the important ones in the brief's risks section rather than the matrix.

## Types

| type | examples | answered by |
|---|---|---|
| instruction | describe the approach, provide references, identify key personnel | a proposal section |
| evaluation | factors, points, weights, adjectival ratings | the section that scores for it |
| format | page, word, font, margin, page size, file type and size | the submission checklist |
| submission | deadlines, delivery method, question procedure | the submission checklist |
| form | signed certifications, required forms, representations | the customer's authorized representative |

## Compliance matrix columns

`req_id, source, section, page, type, requirement, response_section, owner, status`

- `requirement` is the verbatim text; never paraphrase in the matrix.
- `status` is `open` at m1; at m3 it is `addressed`, `open_item` (listed
  under the checklist's Open items), or `needs_review` for abstained
  questionnaire items.
- `owner` is the customer's person for the item when known (intake
  `sme_contacts`), otherwise blank.

## Amendments

Shred the base solicitation and every amendment together (`paths: [...]`).
When an amendment changes a requirement, both rows stay; note in the brief
which one governs.

## Bid/no-bid brief sections

1. **Summary**: what is being bought, by whom, contract type and term.
2. **Key dates**: every deadline in the solicitation (questions, intent to
   bid, proposal due), copied exactly; the check compares this section to
   the source, so put the customer's own dates (internal review, SME
   turnaround) in a separate **Internal schedule** section.
3. **Evaluation criteria**: factors, weights or points, and the award basis.
4. **Risks and open questions**: eligibility doubts, mandatory experience
   the knowledge base may not show, tight page limits, funder AI-use policy,
   unclear instructions to raise through the question procedure.
5. **Recommendation**: bid, no-bid or bid-with-conditions, with the reasons.
   The customer makes the decision.
