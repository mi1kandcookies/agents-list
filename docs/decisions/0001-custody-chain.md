# Decision 0001: chain of custody for agency

Status: accepted for the engagement foundation.

## Purpose

Every money-moving action must be attributable to one fresh human approval of
the exact action. The approval is bound to the structured action, the current
screening verdict, the payer, the payee, the amount in token atomic units, the
chain, and an expiry. A model, agent, browser flag, or stale session cannot
substitute for that approval.

## Custody graph

An `Engagement` is the root of a custody graph. A child engagement represents a
sub-hire and points to its parent. `Mandate` records attenuate authority down
the graph: a child budget cannot exceed the remaining parent budget, categories
must be a subset, `max_depth` cannot increase, `per_tx_max_atomic` cannot
increase, and expiry cannot extend beyond the parent.

`LedgerEntry` is the append-only application audit record for every fund,
release, refund, fee, or sub-hire transfer. It records the authority approval,
screening verdict, addresses, atomic amount, chain, and transaction status.
The simulated executor uses the same interface as the Sepolia executor.

## Approval contract

Approval states are:

```text
created → pending → approved → consumed
                    ├→ denied
                    ├→ expired
                    ├→ cancelled
                    ├→ rejected
                    ├→ blocked
                    └→ failed
```

An approval is single-use. Its `action_hash` is SHA-256 over canonical JSON
containing `approval_id`, integer `exp`, and the complete action terms. The
World ID token `nonce` carries that hash when the identity provider supports
it. Device flow also stores `device_code → action_hash` server-side so a
sandbox that does not echo the nonce cannot detach a device approval from its
action. `jti` is unique and stored before an ID token can be accepted again.

## Screening contract

Screening runs at action creation and again inside `consume()` immediately
before a money-moving executor sends a transaction. Decisions are normalized
to `PAY`, `CAP`, `REFUSE`, or `ASK_HUMAN`. `REFUSE` is terminal for that
attempt. `CAP` records the maximum permitted amount. `ASK_HUMAN` never becomes
an implicit allow. The screening `verdict_id` is included in the action hash.

## Interfaces

The following interfaces are stable boundaries for later work packages:

```python
request_approval(action) -> Approval
validate_identity_callback(approval_id, id_token, device_code=None) -> Human
consume(approval_id, expected_action_hash) -> Approval
screen(action) -> Screening
fund(engagement_id, milestone_id, approval_id) -> LedgerEntry
release(engagement_id, milestone_id, approval_id) -> LedgerEntry
create_child_mandate(parent_mandate_id, caveats) -> Mandate
```

The World client owns OIDC discovery, PKCE, device polling, RS256/JWKS
validation, `auth_time`, pairwise `sub`, and explicit login. The approvals
service owns action binding, replay protection, state transitions, and the
single-use consume boundary. Chain executors own transaction construction and
receipt reconciliation, never identity validation.

## Data model

`Human`, `Engagement`, `Milestone`, `LedgerEntry`, `Approval`,
`ApprovalEvent`, `Mandate`, `Screening`, `EnsName`, and
`UsedIdTokenJti` are separate records. JSON fields store provider evidence and
versioned documents; authorization-critical values also have typed columns.
The existing marketplace `Order` remains readable while new hires use
`Engagement`.

## Security defaults

- Sepolia only; no live transaction is attempted in tests.
- Amounts are integer micro-USDC values.
- Missing screening, identity, ENS, or escrow configuration is a hold/error,
  never a green verdict.
- Protected routes read the server-owned engagement and SOW, not a replacement
  task supplied beside an approval.
- Keys stay outside the model, browser, MCP prompt, and repository.
