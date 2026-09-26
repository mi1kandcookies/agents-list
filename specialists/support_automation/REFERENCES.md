# References

Ideas only. No code, prompts, tool descriptions, fixtures or data were
copied from any of these; everything in this package was written from
scratch.

| source | license | idea used |
|---|---|---|
| sierra-research/tau-bench and tau2-bench (tau3) | MIT | Grade support agents on outcomes rather than wording; keep a versioned, held-out task set; report consistency across repeated trials. Shapes the sealed held-out split and the outcome-based escalation replay. |
| sierra-research/hyper-tau-bench | MIT | Treat "build a support agent from evidence" as its own job, scored by how the built agent does on held-out tasks; sandboxed construction without general internet. Shapes the milestone plan and the no-egress default. |
| emcie-co/parlant | Apache-2.0 | Express agent behavior as condition-to-action guidelines and restrict sensitive flows to approved responses. Shapes `escalation_rules` and `canned_only_intents` in the agent configuration. |
| openai/openai-cs-agents-demo | MIT | Small, focused policies per intent and separate guardrails that run before answering. Shapes the per-intent automation level and must-escalate rules. |
| chatwoot/chatwoot (MIT core and public docs only; the proprietary enterprise directory was not read) | MIT (core) | Mine articles from resolved conversations into an approval queue; flag replies that promise what policy does not allow. Shapes the contradiction finder over agent replies and the draft-only, human-approved KB changes. |
| onyx-dot-app/onyx (community edition docs only) | MIT (community edition) | Citation-first answers and keeping internal-only notes out of customer-facing content. Shapes article `sources` front matter and the grounding check. |
| RAGAS (vibrantlabsai/ragas) | Apache-2.0 | Faithfulness of answers to retrieved context as a first-class metric. Shapes the articles rubric's grounding criterion. |
| Moffatt v. Air Canada, 2024 BCCRT 149 | public decision | A company is bound by its chatbot's statements about refund policy. Motivates grounding every policy statement and escalating refund exceptions. |
| Luhn checksum (ISO/IEC 7812-1) | public standard | Card-number detection only when the digit run passes the checksum, to avoid redacting order ids. |
