# Larkspur Pantry (synthetic)

A fictional meal-kit subscription company. Everything here is invented for
tests and evals: 70 tickets, four help-center articles and three internal
policy documents. Names, emails (example.com) and phone numbers (555-01xx)
are fake; the one card number is a public test number.

Copy `inputs/` into an engagement workspace to use it.

Planted issues the specialist should find:
- PII in ticket bodies (emails, a phone number, a test card number)
- refund window: the help center says 30 days, the internal policy and some
  agent replies say 14 days
- gaps: pause, cancel, delivery status, address change, allergens and
  billing have no help-center article
- must-escalate tickets: chargebacks, disputes, a lawyer threat, food
  illness, account takeover, bereavement

`reference/intent_rules.json` is one acceptable rule set (coverage above
90%). It is used by tests and is not part of the specialist's inputs.
