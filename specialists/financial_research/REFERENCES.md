# References

Ideas only. No code, prompts, tool descriptions, fixtures or data were copied
from any of these; everything here was written from scratch. All fixtures are
synthetic and describe fictional companies.

| source | license | idea used |
|---|---|---|
| SEC EDGAR APIs (`data.sec.gov` submissions and XBRL companyfacts) and SEC fair-access guidance | US government public data | Data shapes for filings and facts; declared User-Agent with contact email; caching to keep request volume low |
| [dgunning/edgartools](https://github.com/dgunning/edgartools) | MIT | Treat filings and XBRL facts as typed records and map tags to statement lines; we built a thin client for the two JSON endpoints instead of taking a dependency |
| [AI4Finance-Foundation/FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | Apache-2.0 | Numbers are computed by code and narrated by the model; provenance for every figure; bull and bear cases argued separately |
| [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher) | Apache-2.0 | Break the research question into sub-questions with per-question source tracking; local documents and public sources side by side |
| [stanford-oval/storm](https://github.com/stanford-oval/storm) | MIT | Generate questions from several perspectives; outline first, then sentence-level citations that a checker can verify |
| [vals-ai/finance-agent](https://github.com/vals-ai/finance-agent) | MIT | A small, sharp tool set (filings search, parse, recall) is enough; keep a question set as a regression eval |
| [virattt/dexter](https://github.com/virattt/dexter) | unclear (README claims MIT, no LICENSE file) | Ideas from its README only: domain know-how as markdown playbooks loaded into the prompt; an explicit ask-the-human step before consequential choices |
| Research on financial QA benchmarks (FinQA, FinanceBench, Vals Finance Agent) | various; FinanceBench is CC-BY-NC | Eval design only (numeric exact-match from XBRL, red-flag recall); no benchmark data is included |

Not used: OpenBB (AGPL-3.0) was deliberately not consulted for code.
