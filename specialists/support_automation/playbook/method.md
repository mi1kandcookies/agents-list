# Method: from tickets to a measured support agent

## M1 - Discovery

1. Check the export: `ticket_id`, `subject`, `body` and a 1/0
   `must_escalate` label on every ticket. If the label is missing, ask the
   client now; milestone 3 cannot be measured without it.
2. `redact_tickets` on `inputs/tickets.csv` -> `deliverables/m1-discovery/tickets_redacted.csv`.
   Confirm `scan_pii` reports 0 on the output.
3. `split_eval_set` (fraction 0.3, seed `support-automation`) before reading
   any ticket. It writes `build_split.csv` and `eval_holdout.csv`; from here
   on read and sample only `build_split.csv`.
4. Skim 30-50 build tickets across channels and months. Draft
   `intent_rules.json`: 8-25 intents, each with an id (snake_case), a label,
   3-8 lowercase keywords or short phrases (at least three characters; a
   keyword matches at the start of a word, so `sue` does not match `issue`),
   and an automation level:
   - `answer_only` - a correct article fully resolves it
   - `with_tools` - needs a backend action (order lookup, refund, plan change)
   - `human_only` - judgment, liability or empathy required (disputes, legal,
     safety, bereavement, harassment)
5. `build_intent_taxonomy`. Read the unclassified build tickets it lists and
   samples of the largest intents; refine keywords. Stop when coverage >= 90%
   and labels read right. Prefer specific phrases over single common words
   ("cancel my subscription" beats "cancel").
6. `kb_coverage` against `inputs/help_center`. Declared `intents:` front
   matter counts as coverage; otherwise two keyword hits are needed.
7. `find_contradictions` with the policy terms that matter to this business
   (refund, cancel, shipping, deliver, return, trial, warranty ...), passing
   the ticket export so agent replies are compared with articles.
8. Write `discovery_report.md` with sections: Summary, Intent taxonomy,
   Knowledge gaps, Contradictions, Automation candidates, Open questions.

## M2 - Knowledge gap fill

The m1 files are pinned to their submitted versions. Do not re-run the m1
tools into their default paths; write any new output under
`deliverables/m2-knowledge/`.

1. Take the top automatable gaps from the gap map (skip `human_only`).
2. For each gap, find grounding: policy docs first, then current articles,
   then resolved build-split tickets with consistent answers (for steps only;
   a ticket never grounds a number).
3. Write one article per gap under `deliverables/m2-knowledge/articles/`
   (Markdown files only, no subfolders) with front matter `title`,
   `intents`, `sources` (see article-style.md).
4. Write macros (`macros.json`) for intents where an agent reply template
   helps: `{"id", "title", "intents", "body", "sources"}`. Placeholders such
   as `{{first_name}}` are allowed in macro bodies only.
5. Resolve contradictions: pick the value the policy owner confirms (ask if
   unknown) and state only that value. Run `find_contradictions` over the
   new articles and macros; it must come back empty.
6. `change_log.md`: every new or changed article/macro, why, and its sources.

## M3 - Agent configuration and evaluation

The m1 and m2 files are pinned to their submitted versions, including the
sealed split.

1. Write `agent_config.json`:
   `instructions`, `tone`, `ai_disclosure`, `handoff_message`,
   `knowledge_paths` (help center and articles, never the ticket export),
   `intents` (id -> automation and article/macro),
   `escalation_rules` [{category, keywords, reason}], `canned_only_intents`.
   Cover the five default categories and every category the client added at
   intake (the acceptance criteria list the exact ids).
2. Tune escalation keywords on `build_split.csv` only, then run
   `replay_escalations` once on `eval_holdout.csv`. Recall must be >= 95%
   and precision >= 30%. If not, report the misses honestly; do not tune on
   held-out tickets.
3. `eval_report.md`: Summary, Configuration, Escalation replay (with the
   number of held-out must-escalate tickets and the recall's lower bound),
   Known limitations, Go-live prerequisites.
