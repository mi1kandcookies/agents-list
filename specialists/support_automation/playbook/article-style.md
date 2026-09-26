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
- State limits (days, amounts) exactly as the policy states them.
- No internal-only details (tool names, agent notes, other customers).
- Every policy statement traces to a listed source. Ticket sources use the
  row anchor `inputs/tickets.csv#<ticket_id>` and must come from the build
  split, never the held-out set.

## Macro checklist

- Greets, answers, states the next step, closes. Under 120 words.
- Uses placeholders only for customer-specific values (`{{first_name}}`,
  `{{order_id}}`).
- Never promises what policy does not allow; links the relevant article.
- `human_only` intents get a handoff macro, not an answer macro.
