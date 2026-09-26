# References

Ideas only. No code, prompts, tool descriptions, fixtures or data were
copied from any of these projects; everything here was written from
scratch.

| Source | License | Idea used |
|---|---|---|
| Canner/WrenAI | Apache-2.0 for core (docs CC-BY-4.0; future modules reserved AGPL-3.0) | Keep metric meaning (grain, filters, owner) in reviewable files next to the SQL; profile real values before writing filters |
| agno-agi/dash | Apache-2.0 | Enforce read-only access in the tool and database configuration, not in the prompt; build a library of validated queries as a by-product of the engagement |
| vanna-ai/vanna | MIT (archived) | Cautionary example: never execute model-generated code in the host process; this pack runs no generated code at all, only guarded SQL |
| Snowflake-Labs/ReFoRCE (arXiv 2502.00675) | Apache-2.0 | Cross-check a result by a second query route before reporting it (playbook method step 4) |
| microsoft/data-formulator | MIT | Keep a provenance trail from each reported value back to the transformation that produced it (figures.json -> saved query) |
| evidence-dev/evidence | MIT | Treat the deliverable as SQL plus markdown that can be rebuilt and checked, with "numbers match the source" as the acceptance test |
| Spider 2.0, DABstep (CC-BY-4.0), InfiAgent-DABench (Apache-2.0) | as listed | Execution-based scoring: acceptance re-runs queries and compares results instead of grading prose |
