# Article and macro style

## Article format

```markdown
---
title: Change your delivery address
intents: delivery_address_change
sources: inputs/policies/shipping.md, inputs/tickets.csv#T0107
---
# Change your delivery address

One-sentence answer first.

## Steps
1. ...

## If this does not work
When to contact support and what to include.
```

- One task per article; the title is the customer's question as an action.
- Answer in the first sentence. Steps are numbered and start with a verb.
- State limits (days, amounts) exactly as the policy states them. Every
  limit must appear in a cited policy document or help-center article; a
  ticket never grounds a number (customers and one-off replies are not
  policy).
- No internal-only details (tool names, agent notes, other customers).
- Every policy statement traces to a listed source. Ticket sources use the
  row anchor `inputs/tickets.csv#<ticket_id>` (never the whole export) and
  must come from `build_split.csv`, never the held-out set.
- Only Markdown files go in `articles/`; no subfolders.

## Macro checklist

- Greets, answers, states the next step, closes. Under 120 words.
- Uses placeholders only for customer-specific values (`{{first_name}}`,
  `{{order_id}}`).
- Never promises what policy does not allow; links the relevant article.
- `human_only` intents get a handoff macro, not an answer macro.
