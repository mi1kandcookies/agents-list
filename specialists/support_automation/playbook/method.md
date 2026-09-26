# Method: from tickets to a measured support agent

## M1 - Discovery

1. `redact_tickets` on `inputs/tickets.csv` -> `deliverables/m1-discovery/tickets_redacted.csv`.
   Confirm `scan_pii` reports 0 on the output.
2. Skim 30-50 redacted tickets across channels and months. Draft
   `intent_rules.json`: 8-25 intents, each with an id (snake_case), a label,
   3-8 lowercase keywords or short phrases, and an automation level:
   - `answer_only` - a correct article fully resolves it
   - `with_tools` - needs a backend action (order lookup, refund, plan change)
   - `human_only` - judgment, liability or empathy required (disputes, legal,
     safety, bereavement, harassment)
3. `build_intent_taxonomy`. Read samples of `other` and of the largest
   intents; refine keywords. Stop when coverage >= 90% and labels read right.
   Prefer specific phrases over single common words ("cancel my
   subscription" beats "cancel").
4. `kb_coverage` against `inputs/help_center`. Declared `intents:` front
   matter counts as coverage; otherwise two keyword hits are needed.
5. `find_contradictions` with the policy terms that matter to this business
   (refund, cancel, shipping, delivery, return, trial, warranty ...), passing
   the ticket export so agent replies are compared with articles.
6. Write `discovery_report.md` with sections: Summary, Intent taxonomy,
   Knowledge gaps, Contradictions, Automation candidates, Open questions.

## M2 - Knowledge gap fill

1. Take the top automatable gaps from the gap map (skip `human_only`).
2. For each gap, find grounding: policy docs first, then current articles,
   then resolved tickets from the build split with consistent answers.
3. Write one article per gap under `deliverables/m2-knowledge/articles/`
   with front matter `title`, `intents`, `sources` (see article-style.md).
4. Write macros (`macros.json`) for intents where an agent reply template
   helps: `{"id", "title", "intents", "body", "sources"}`. Placeholders such
   as `{{first_name}}` are allowed in macro bodies only.
5. Resolve contradictions: pick the value the policy owner confirms (ask if
   unknown) and state only that value. Run `find_contradictions` over the
   new articles and macros; it must come back empty.
6. `change_log.md`: every new or changed article/macro, why, and its sources.

## M3 - Agent configuration and evaluation

1. `split_eval_set` on `tickets_labeled.csv` (fraction 0.3, seed
   `support-automation`) before tuning anything.
2. Write `agent_config.json`:
   `instructions`, `tone`, `ai_disclosure`, `handoff_message`,
   `knowledge_paths`, `intents` (id -> automation and article/macro),
   `escalation_rules` [{category, keywords, reason}], `canned_only_intents`.
3. Tune escalation keywords on the build split only, then run
   `replay_escalations` once on the held-out split. Recall must be >= 95%.
   If it is not, report the misses honestly; do not tune on held-out tickets.
4. `eval_report.md`: Summary, Configuration, Escalation replay, Known
   limitations, Go-live prerequisites.
