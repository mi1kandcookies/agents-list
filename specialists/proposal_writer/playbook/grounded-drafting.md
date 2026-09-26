# Playbook: evidence map, outline and grounded draft (m2-outline, m3-draft)

## Evidence map

`evidence_map.csv` columns: `req_id, type, status, passages, note`.

- `mapped`: one or more `<file>#p<N>` passage ids, separated by `;`, each of
  which you have read and which actually supports the requirement. At least
  one must share a word with the requirement (its file name counts); a
  match on figures alone is not evidence.
- `gap`: nothing in the knowledge base supports it. The requirement id must
  appear in `gaps.md` with a specific question and, when known, who should
  answer it ("R-020: Which two projects completed since 2021 best match the
  scope, and who at each customer will act as a reference?").
- `not_applicable`: only for format, submission and form rows.

Lexical search misses synonyms: search again with the evaluator's terms and
the company's terms before declaring a gap.

## Annotated outline

```
# Volume I Technical
## Technical Approach [pages: 3] [covers: R-002, R-003, R-019, R-023]
```

- Top-level headings are the volumes named in the instructions, in their
  words; section order follows the instructions, not the statement of work.
  A page limit applies to the headings its sentence names ("The Technical
  Volume shall not exceed ten pages" -> "Volume I Technical"), at any level.
- Every instruction and evaluation requirement appears in exactly one
  section's `[covers: ...]` list (more than one only when the instructions
  ask for it in several places).
- Budget pages by evaluation weight; the sum per volume must not exceed the
  volume's page limit.
- Under each heading, one or two lines on the win theme and the evidence
  (passage ids) the section will use.

## Draft conventions

- Each section starts with `<!-- R-###, R-### -->` listing the requirements
  it answers, and answers them in prose (a marked section with under ten
  words answers nothing). `update_compliance_matrix` turns these into
  response locations and reports markers that did not count.
- Company facts carry `[KB:<file>#p<N>]`. The cited passage must contain
  every number and certification the sentence states, in the same form
  ("$640,000", not "$0.64 million").
- Restating the solicitation's own figure: put `[REQ:R-###]` right after it,
  in requirement words ("within the 72 hour window [REQ:R-008]"), next to
  the KB-cited fact. `[REQ:]` never grounds a company fact or a
  certification, and a sentence restating only the requirement's figure
  still needs the KB-cited fact that meets it.
- Commitments about future work ("the app will support trip planning") need
  no citation as long as they state no company fact.
- Never state a certification, award, customer count, revenue figure,
  percentage or staff count without a citation. If it is not in the
  knowledge base, it is a gap.
- The submission checklist carries markers for the format, submission and
  form requirements (a checklist marker does not answer any other kind) and
  ends with a `## Open items` section that names, by id, every requirement
  the draft cannot answer yet, plus pricing inputs and signatures. The
  matrix shows those rows as `open_item`.

## Default positions

- Executive summary: answer "why this team, for this buyer, now" in the
  buyer's terms; one proof point per claim.
- Past performance: name, period, scope, outcome, reference contact role,
  all from the knowledge base; relevance to this scope in one sentence.
- Key personnel: role, years of relevant experience, one directly relevant
  accomplishment, from the resume on file.
- Pricing: structure only; numbers come from the customer.
