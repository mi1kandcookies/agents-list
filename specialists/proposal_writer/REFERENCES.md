# References

Ideas only; no code, prompts, tool descriptions, fixtures or data were
copied. Everything in this package is original.

| Source | License | Idea used |
|---|---|---|
| scorpionus007/QResponder | Apache-2.0 | "Grounded or abstain" questionnaire answering: answer only from retrieved knowledge-base text with a mandatory citation, otherwise mark the item for review; never submit on the customer's behalf. Here: `set_answer` and `questionnaire_answers_grounded`. |
| trycompai/comp | AGPL-3.0 (read from public docs only) | Questionnaire pipeline shape: parse the upload, retrieve, answer with citations, export. |
| microsoft/agent-for-rfp-response-solution-accelerator | MIT | Always produce a compliance-and-security section from a policy knowledge base; synthetic sample knowledge bases for demos and evaluation. |
| blencorp/capture-mcp-server | MIT | A verification step the writer must pass before asserting a fact; here the grounding scan that checks each cited number against its passage. |
| UABGH-Emerging-Technologies/Grant_Guide_Publication | GPL-3.0 (concept from README only) | Section-by-section drafting keyed to the funder's review criteria, and stating plainly that output is a starting draft. |
| Okapi BM25 (Robertson and Zaragoza, "The Probabilistic Relevance Framework: BM25 and Beyond", 2009) | paper | Keyword ranking for knowledge-base passages, implemented from the published formula. |
| Proposal-management practice (requirement shred, compliance matrix, Section L/M cross-walk) | common practice | Shred binding "shall/must/will" statements into numbered requirements with section and page, and trace each to a response location. |

Reviewed and deliberately not used: a small repository advertising a
multi-agent RFP pipeline whose download instructions matched malware-lure
patterns (binary download from a source path, instructions to bypass
operating-system warnings). Nothing from it was downloaded or run.
