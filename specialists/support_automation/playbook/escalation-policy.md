# Default escalation positions

An automated agent must hand these to a person every time. The customer can
add categories at intake but should not remove these without the policy
owner's written approval.

| category | typical signals | why |
|---|---|---|
| `billing_dispute` | chargeback, dispute, bank reversal, "charged twice", fraud on card | money movement and liability |
| `legal_threat` | lawyer, attorney, sue, small claims, regulator, complaint to authority | legal exposure; wording matters |
| `safety` | injury, burn, allergic reaction, unsafe, fire, recall | physical harm |
| `account_security` | hacked, someone else logged in, unauthorized, takeover, changed my email | identity must be verified by a person |
| `vulnerable_user` | bereavement, passed away, self-harm, abuse, "can't afford food" | empathy and care beyond scripts |

## Rules of thumb

- Recall beats precision. A false escalation costs a few minutes of a
  person's time; a missed one can cost a customer, a chargeback or a lawsuit.
  Still, a rule set that hands most conversations to a person is not an
  automation: the held-out replay needs at least 30% precision.
- Keywords are phrases of at least three characters in a list. They match
  at the start of a word ("sue" matches "sued", not "issue"), so a stem
  catches its inflections; prefer phrases over single common words.
- The agent never promises refunds, credits or exceptions that no approved
  policy states. If the policy has a limit (amount, days), amounts above the
  limit escalate.
- No account changes (email, password, payout details) without verification
  by a person or a verified tool flow.
- In regulated verticals (health, financial services, children's services)
  the escalation list and disclosures need the customer's compliance review.
- The configuration must include AI-disclosure text shown at the start of
  every conversation, and a handoff message that tells the customer what
  happens next.
