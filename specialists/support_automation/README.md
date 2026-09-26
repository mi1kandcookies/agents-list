# Support Automation Builder (`support-automation`)

A vendor-neutral builder for AI support agents. It reads a company's ticket
history and help center, finds what customers actually ask and what the
knowledge base cannot answer, drafts grounded articles and macros for the
biggest gaps, and delivers a support-agent configuration whose escalation
rules are measured on held-out tickets. The held-out split is sealed right
after redaction, before the specialist reads any ticket, and is never cited.

It builds and measures. It never publishes articles, activates macros or
bots, or replies to customers.

## Milestones

| id | deliverables (`deliverables/<id>/`) | automated acceptance |
|---|---|---|
| `m1-discovery` | `tickets_redacted.csv`, `build_split.csv`, `eval_holdout.csv`, `intent_rules.json`, `tickets_labeled.csv`, `intent_taxonomy.csv`, `kb_gap_map.csv`, `discovery_report.md` | export has `must_escalate` labels and unique ids; the redacted copy keeps every ticket and cell (only redacted) and holds no emails, phones or card numbers; the split is the seeded, stratified split of the export; every taxonomy row, ticket label and gap-map row re-derives from the export, with >= 90% coverage; report sections and disclaimer |
| `m2-knowledge` | `articles/*.md`, `macros.json`, `change_log.md` | m1 files unchanged since submitted; top 5 automatable gaps (volumes from the export) each get an article or macro, and not every gap may be `human_only`; every article cites client files under `inputs/` or ticket rows (never the whole export) and states only policy numbers found in a cited client document; macros valid under the same rule; no policy term with two different numbers; no held-out ticket cited; ledger quotes verified; no PII or placeholders; disclaimer |
| `m3-agent-config` | `agent_config.json`, `escalation_replay.json`, `eval_report.md` | m1 and m2 files unchanged since submitted; config has instructions, AI disclosure, handoff and well-formed rules for every must-escalate category (the five defaults plus the client's own); split still sealed and never cited; must-escalate recall >= 95% over at least 3 held-out must-escalate tickets, precision >= 30%, both equal to the delivered report; disclaimer |

Each milestone also has a rubric review (`rubrics/*.yaml`) and a human
sign-off by the customer's support lead or policy owner.

## Inputs

- `inputs/tickets.csv` - ticket export: `ticket_id`, `subject`, `body` and
  `must_escalate` (1/0 on every ticket: did it have to go to a person?);
  `agent_reply`, `handle_minutes` and `status` help. Milestone 3 cannot pass
  without the labels, and m1 fails early if they are missing. Excel's
  "CSV UTF-8" (with a byte-order mark) is fine.
- `inputs/help_center/*.md` (or `.txt`) - current articles (front matter
  `intents:` is optional).
- `inputs/policies/` - internal policy documents (optional, strongly
  recommended: an article may only state a limit that a cited document
  states).

Intake also asks for the target helpdesk platform, extra must-escalate
categories (they become required escalation rules in m3), whether the
vertical is regulated (if so, a named compliance approver is required
before work starts), and how many gaps to fill.

## Domain tools (`tools.py`)

`redact_tickets`, `scan_pii`, `split_eval_set`, `build_intent_taxonomy`,
`kb_coverage`, `find_contradictions`, `replay_escalations`. All are
deterministic and offline. Intent and escalation keywords match at the
start of a word and need at least three characters.

## Domain checks (`checks.py`)

`ticket_export_valid`, `no_pii_remaining`, `redaction_complete`,
`taxonomy_reconciles`, `gap_map_consistent`, `top_gaps_addressed`,
`articles_grounded`, `macros_valid`, `kb_consistent`,
`agent_config_valid`, `eval_holdout_sealed`, `escalation_recall`,
`prior_milestone_unchanged`. Checks recompute from the client's files in
`inputs/`, so a hand-edited redacted copy, taxonomy, gap map, held-out set
or replay report fails, and later milestones cannot rewrite the files an
earlier milestone submitted.

## Wiring (`agent.py`)

`SupportAutomation` runs on the shared kit (`python -m agentkit run
support-automation ...`). The domain tools get the kit's `resolve_path`, so
they obey the same policy as the builtin file tools (`inputs/` read-only,
`.agentkit/` off-limits, written files never become ledger sources). On top
of the defaults it:

- requires a named compliance approver when the intake declares a
  regulated vertical;
- raises m2's `top_gaps_addressed` count when the intake's `top_n_gaps` is
  larger than 5 (the manifest's 5 stays the floor), and adds the intake's
  extra must-escalate categories to m3's required escalation rules;
- creates `deliverables/m2-knowledge/articles/` before the run and hashes
  every file in it into the submission's artifacts;
- lets `rubric_grader` and `no_placeholders` take the articles folder in
  `paths` and check each file in it.

## Human gate

No licensed reviewer is required for general commercial support, so the
manifest's `human_gate.required` is false. Every report carries the draft
disclaimer and every deliverable is still a draft: the support lead approves
articles and macros; the policy owner approves refund, credit, cancellation,
account-security and escalation wording; in regulated verticals (health,
financial services, children's services) the compliance approver named at
intake approves escalation rules and disclosures. The customer alone
publishes content or turns on an agent.

## Limits

- No network egress and no shell; everything runs on the uploaded exports.
- Redaction covers email addresses, phone numbers (North American,
  international with `+`, national with a leading 0, bare 10-11 digit runs)
  and Luhn-valid card numbers in every column but `ticket_id`. It does not
  detect names or street addresses; the specialist is told to keep them out
  of deliverables and the support lead should spot-check.
- Intent labeling is keyword-rule based so volumes are reproducible; the
  specialist refines the rules, the tool applies them.
- Escalation replay measures keyword rules only. With few held-out
  must-escalate tickets the recall figure is a smoke test (the report gives
  its 95% lower bound: 3 of 3 is only 0.44); it is not a substitute for
  simulated-conversation testing or a shadow-mode pilot before go-live,
  which are outside these three milestones.
- Contradiction detection compares stated numbers (days, hours, amounts,
  percentages) per policy term, clause by clause; wording-level conflicts
  need human review.
- The pin on earlier milestones needs their submissions in the same
  workspace; a milestone run on its own (for example an eval case) builds
  the earlier files itself and they are not pinned.
- Budget per milestone: 120 steps, 4M tokens, $50, 8 hours wall time.

## Evals

`evals/fixtures/larkspur/` is a fictional meal-kit company with planted PII,
a refund-window contradiction, knowledge gaps, must-escalate tickets, a
prompt injection and a one-off refund promise.
`evals/cases/*.json` hold one case per milestone for live-model evals.
