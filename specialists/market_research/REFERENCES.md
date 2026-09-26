# References

Ideas only; no code, prompts, tool descriptions or data were copied. Every
file in this package was written from scratch.

| Source | License | Idea used |
|---|---|---|
| assafelovic/gpt-researcher | Apache-2.0 | Rank source credibility before synthesis (our source tiers); a reviewer pass against an explicit guideline list (our rubrics and acceptance checks) |
| stanford-oval/storm (and the STORM / Co-STORM papers, arXiv 2402.14207, 2408.15232) | MIT | Perspective-guided questioning (buyer, competitor, entrant, regulator, investor vantage points in the question tree); outline first, then write each section from that section's evidence |
| langchain-ai/open_deep_research | MIT | An explicit research brief as the contract between intake and execution (our m1 question tree and plan) |
| dzhng/deep-research | MIT | Compact atomic learnings carried between steps (our atomic ledger claims); clarifying questions up front (`ask_client`) |
| bytedance/deer-flow | MIT | Provenance receipts where every output claim points back to a recorded tool result (our claim IDs and snapshot hashes) |
| lajosdeme/mole | Apache-2.0 | Verbatim-quote gate at extraction time; mark unsupported claims instead of silently dropping them; explicit contradictions section |
| DeepResearch Bench (arXiv 2506.11763), DeepConsult (youdotcom-oss/ydc-deep-research-evals) | Apache-2.0 / MIT | Report-quality criteria (comprehensiveness, insight, instruction following, clarity) informing `rubrics/report.yaml`; citation accuracy as a separate, checkable metric |
