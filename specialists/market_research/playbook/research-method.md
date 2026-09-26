# Research method

## Question tree

- One root per decision the client must make. Two to six key questions under
  it; two to five leaf questions under each key question.
- A leaf is answerable with evidence in one sentence or one number ("What did
  the three largest vendors charge per seat for the mid tier on the cutoff
  date?"), not a topic ("pricing").
- Each leaf carries `source_types` (at least two) and names at least one
  primary type: `filing`, `government`, `company`, `standard`, `client`.
  Secondary types: `trade_press`, `analyst`, `news`, `review`, `academic`,
  `community`.
- Vantage points to walk before freezing the tree: buyer (why switch, budget
  owner), competitor (positioning, pricing moves), new entrant (barriers),
  regulator (rules that change the market), investor (growth, margins).

`questions.json` shape (validated by `write_question_tree`):

```json
{"objective": "...", "evidence_cutoff": "2026-09-01",
 "questions": [{"id": "Q1", "text": "...", "children": [
   {"id": "Q1.1", "text": "...", "source_types": ["filing", "trade_press"]}]}]}
```

## Evidence gathering

- Search broadly, fetch narrowly. Fetch the page that actually states the fact.
- Record the source once, then one claim per atomic fact. A claim's `text` is
  your wording; its `quote` is copied character for character from the page.
- Tier every source with `classify_source`: tier 1 = primary (filings,
  government statistics, the company's own pages, standards bodies, client
  documents), tier 2 = reputable secondary (trade press, analysts, academic),
  tier 3 = everything else (blogs, forums, aggregators).
- Stop gathering for a leaf once it has three independent claims, or mark it
  `insufficient evidence` in the claim map notes with what you tried.

## Contradiction log

`contradictions.md` lists every disagreement: the metric, each side with its
claim ID and source tier, which one the report uses and why (recency, tier,
method). Unresolved conflicts go into the report's "Contradictions and open
questions" section.

## Report

Required sections: Executive summary, Market definition, Market size,
Competitive landscape, Contradictions and open questions, Traceability matrix,
Sources. The traceability matrix is a table with one row per leaf question:
question ID, finding (or `unresolved`), claim IDs.
