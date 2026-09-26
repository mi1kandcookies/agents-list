# Research method

## M1 - plan, peer set, sources

Write `deliverables/m1-plan-sources/research_plan.md` with these sections:

- **Objective** - the decision this supports, in one or two sentences.
- **Key questions** - three to eight specific questions, each mapped to the
  deliverable that answers it (spreads, comps, red flags, memo section).
  Generate them from several perspectives: an owner or acquirer, a competitor,
  a lender, a regulator, a customer. Keep the ones that change the decision.
- **Peer set** - a table: company, CIK, ticker, why included (business model,
  end market, scale, geography, growth profile), caveats (fiscal year end,
  segment mix, foreign filer). List notable exclusions and why.
- **Sources** - what is primary (10-K, 10-Q, 8-K, XBRL companyfacts, data-room
  documents), what is secondary, and what is missing.
- **Plan and timeline** - the next milestones, what the client must approve
  before M2, and what is out of scope (ratings, price targets, contacting
  management, licensed data the client has not supplied).

Then:

1. `edgar_submissions` for every company (confirms name, CIK, fiscal year end,
   filing history) and `edgar_companyfacts` for every company.
2. `index_dataroom` (writes the index even when there is no data room).
3. Write `source_inventory.csv` with columns
   `source_id, kind, company, cik, form, accession, filed, uri, sha256`:
   - `edgar_filing`: the 10-Ks (and any 10-Qs or 8-Ks you will cite), with the
     accession, form and filing date exactly as the filing index shows and the
     document URL from `edgar_submissions`;
   - `edgar_api`: the data.sec.gov submissions and companyfacts URLs you used;
   - `dataroom`: each data-room file you rely on, with its sha256 from the index;
   - `web`: anything else, only when the client allowed it.
4. Ask the client to approve the peer set and questions (human sign-off).

## Default positions

- Peers: four to eight. Fewer than three makes medians meaningless; more than
  ten usually means the set was not curated.
- Fiscal years: the last three completed fiscal years, plus the year before the
  first for growth.
- Source tiers: filings and XBRL first, then data-room documents, then client
  statements. Press and web pages only as context, labelled as secondary.
- A company without XBRL financials (some foreign private issuers, very small
  filers) stays in the plan with a note; its figures come from quoted filing
  text recorded in the ledger, not from companyfacts.
