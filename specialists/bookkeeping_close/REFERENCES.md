# References

Ideas only. No code, prompts, tool descriptions, fixtures or data were
copied; everything here was written from first principles. Copyleft
projects were consulted through their public documentation only.

| source | license | idea used |
|---|---|---|
| anthropics/financial-services (month-end close and GL reconciliation examples) | Apache-2.0 | Keep all reconciliation math in deterministic code; treat "must foot, never plug" as a hard failure; cite what each line ties to; stage entries as drafts rather than posting; bucket breaks by likely cause (timing, duplicate, fee, mapping) |
| anthropics/knowledge-work-plugins (finance and small-business plugins) | Apache-2.0 | Gated close sequence where later steps run only on signed-off numbers; exceptions queue as a first-class deliverable; never merge payees automatically; plain-English owner summary alongside reviewer workpapers; ledger-neutral CSV inputs |
| beancount/smart_importer | MIT | Rules and the customer's own history as a cheap first-pass categorizer, with the model used only for low-confidence or novel lines |
| jbms/beancount-import | GPL-2.0 (docs only) | Match the same money movement across sources before creating anything new; rank candidates and ask a human on ambiguity |
| intuit/quickbooks-online-mcp-server, XeroAPI/xero-mcp-server | Apache-2.0 / MIT | Remove mutating capabilities entirely in read-only mode (here: no posting tool exists at all) |
| vas3k/TaxHacker | MIT | Schema-constrained, provider-neutral extraction of amounts from documents (deferred to a later version) |
| Published accounting-agent benchmark write-ups on multi-month closes | n/a | The failure mode to design against: agents drifting across months and forcing reconciliations with unsupported entries, hence recompute-from-source checks and the no-plug gate |
