# Support Automation Builder (`support-automation`)

A vendor-neutral builder for AI support agents. It reads a company's ticket
history and help center, finds what customers actually ask and what the
knowledge base cannot answer, drafts grounded articles and macros for the
biggest gaps, and delivers a support-agent configuration whose escalation
rules are measured on tickets it never saw while building.

It builds and measures. It never publishes articles, activates macros or
bots, or replies to customers.

## Milestones

| id | deliverables (`deliverables/<id>/`) | automated acceptance |
|---|---|---|
| `m1-discovery` | `tickets_redacted.csv`, `intent_rules.json`, `tickets_labeled.csv`, `intent_taxonomy.csv`, `kb_gap_map.csv`, `discovery_report.md` | redaction keeps every ticket and leaves no PII; taxonomy volumes re-derive from the export with >= 90% coverage; gap map re-derives from the help center; report sections |
| `m2-knowledge` | `articles/*.md`, `macros.json`, `change_log.md` | top 5 automatable gaps each get an article or macro; every article cites sources that resolve; macros valid; no policy term with two different numbers; ledger quotes verified; no PII |
| `m3-agent-config` | `agent_config.json`, `eval_holdout.csv`, `build_split.csv`, `escalation_replay.json`, `eval_report.md` | config has instructions, AI disclosure, handoff and rules for every must-escalate category; held-out split matches the seed and is never cited; must-escalate recall >= 95% on the held-out set and equal to the delivered report |

Each milestone also has a rubric review (`rubrics/*.yaml`) and a human
sign-off by the customer's support lead or policy owner.

## Inputs

- `inputs/tickets.csv` - ticket export: `ticket_id`, `subject`, `body`;
  ideally `agent_reply`, `handle_minutes`, `status` and a `must_escalate`
  (1/0) label for the held-out replay.
- `inputs/help_center/*.md` - current articles (front matter `intents:` is
  optional).
- `inputs/policies/` - internal policy documents (optional, strongly
  recommended for grounding).

Intake also asks for the target helpdesk platform, extra must-escalate
categories, whether the vertical is regulated, and how many gaps to fill.

## Domain tools (`tools.py`)

`redact_tickets`, `scan_pii`, `build_intent_taxonomy`, `kb_coverage`,
`find_contradictions`, `split_eval_set`, `replay_escalations`. All are
deterministic and offline.

## Domain checks (`checks.py`)

`no_pii_remaining`, `redaction_complete`, `taxonomy_reconciles`,
`gap_map_consistent`, `top_gaps_addressed`, `articles_grounded`,
`macros_valid`, `kb_consistent`, `agent_config_valid`,
`eval_holdout_sealed`, `escalation_recall`. Checks recompute from the source
files, so a hand-edited taxonomy, gap map or replay report fails.

## Human gate

No licensed reviewer is required for general commercial support, so the
manifest's `human_gate.required` is false. Every deliverable is still a
draft: the support lead approves articles and macros; the policy owner
approves refund, credit, cancellation, account-security and escalation
wording; in regulated verticals (health, financial services, children's
services) the customer's compliance reviewer approves escalation rules and
disclosures. The customer alone publishes content or turns on an agent.

## Limits

- No network egress and no shell; everything runs on the uploaded exports.
- Intent labeling is keyword-rule based so volumes are reproducible; the
  specialist refines the rules, the tool applies them.
- Escalation replay measures keyword rules only. It is not a substitute for
  simulated-conversation testing or a shadow-mode pilot before go-live,
  which are outside these three milestones.
- Contradiction detection compares stated numbers (days, hours, amounts,
  percentages) per policy term; wording-level conflicts need human review.
- Budget per milestone: 120 steps, 4M tokens, $50, 4 hours wall time.

## Evals

`evals/fixtures/larkspur/` is a fictional meal-kit company with planted PII,
a refund-window contradiction, knowledge gaps and must-escalate tickets.
`evals/cases/*.json` hold one case per milestone for live-model evals.
