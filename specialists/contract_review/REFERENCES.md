# References

Projects and publications whose ideas shaped this specialist. Clean-room:
we read them for ideas only. No code, prompts, tool descriptions, fixtures
or data were copied; all prompts, playbook text, rubrics and fixtures here
are original, and the companies in the fixtures are fictional.

| Source | License | Idea used |
|---|---|---|
| anthropics/claude-for-legal | Apache-2.0 | Playbook as configuration built from an intake interview, with a refusal to review against an empty playbook; separate customer-side and vendor-side positions; liability split into cap amount, cap base, indirect damages and carve-outs; treating a redline as a negotiating tool rather than a redraft; review flags on subjective calls; spreadsheet formula-injection defence. |
| anthropics/knowledge-work-plugins (legal plugin) | Apache-2.0 | A clause-family taxonomy with per-family things to look for; read the whole contract before flagging because clauses interact; a clearly labelled baseline when no playbook exists. |
| dealfluence/adeu | MIT | Redline ops as search/replace plus comment, failing closed on ambiguous matches; comment-only ops; a defined-terms and cross-reference report re-checked after edits. |
| JSv4/Python-Redlines (engine JSv4/Docxodus) | MIT | Treating "reject all equals the original, accept all equals the proposal" as the verification of a tracked-changes document. Our comparison is our own stdlib code; no binary is used. |
| Open-Source-Legal/OpenContracts | MIT (license history changed; ideas from docs only) | Findings anchored to spans in the source text; treating in-document instructions as data. |
| evolsb/claude-legal-skill, evolsb/legal-redline-tools | MIT | A structured JSON redline list handed to a deterministic document engine; severity thresholds kept as editable data; missing-provision and consistency post-passes; internal memo kept separate from counterparty-facing comments. |
| CUAD (Atticus Project) | Dataset CC BY 4.0 | Clause-category coverage as a way to think about recall on critical families; candidate source for a future offline clause-detection eval (not bundled). |
| ContractNLI (Stanford) | CC BY 4.0 | NDA-specific hypotheses as a future eval for the NDA path (not bundled). |
| Vals Legal AI Report (Feb 2025) | Report | Redlining is where human lawyers still lead, which is why the redline is verified mechanically and gated by an attorney. |
| ABA Formal Opinion 512 (July 2024) | Public opinion | Competence, confidentiality and supervision duties used as the design brief for the attorney gate. |
| OOXML (ECMA-376) WordprocessingML | Standard | `w:ins`, `w:del`, `w:delText` and comment ranges for native tracked changes. |
