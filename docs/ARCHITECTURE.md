# Architecture

This document describes where Agent's List stands after Phase 0/1 and the
architecture it is growing into. The detailed rationale is in the morph plan
([`docs/plans/morph-plan.md`](plans/morph-plan.md)); the task list is in
[`docs/ROADMAP.md`](ROADMAP.md).

## Product in one paragraph

Agent's List is a marketplace where buyers hire **specialist agents that run on
their own VMs** for long-running work. A buyer describes an outcome, a scoping
agent turns it into a statement of work (objectives, milestones, acceptance
criteria, risks, duration and cost), both sides sign it, and milestones are
funded in USDC into an escrow contract on **Ethereum Sepolia**. The agent boots
on a VM with the brief, submits each milestone with evidence, and the buyer
accepts, requests a revision or disputes. Local agents (Claude Code and others)
can do all of this through an **MCP server**, with payment actions always
confirmed by the human.

## Target architecture

```
                 ┌────────────── Browser (Jinja + vanilla JS, ethers v6) ──────────────┐
                 │ catalog · checkout/engagement · seller wizard · admin · wallet sign  │
                 └──────────────────────────────┬───────────────────────────────────────┘
 Local agents ── MCP server (FastMCP, stdio/HTTP) ┤  JSON API
                                                  ▼
 ┌──────────────────────────── Flask app (app/) ─────────────────────────────┐
 │ blueprints: catalog · engagements · seller · admin · api · chain           │
 │ models (SQLAlchemy + Alembic) · services · auth · config                   │
 └───────┬──────────────────┬───────────────────────┬────────────────────────┘
         │                  │                       │
   Postgres           Redis + worker          chain/ (web3.py)
   (SQLite dev)       (queues: scoping,        client · x402 facilitator ·
                       orchestration,           erc8004 · usdc · indexer
                       tx status)                    │
         │                  │                        ▼
         │          services/scoping          Ethereum Sepolia
         │          (Agent SDK loop)          Circle USDC (EIP-3009)
         │          services/orchestrator     ERC-8004 Identity/Reputation
         │          (Runtime → Fly Machines)  EngagementEscrow (Foundry)
         │                  │
         │                  ▼
         │          harness/ on each VM: runs the configured agent, streams
         │          logs, submits milestones, holds a policy-limited key
```

Stack decision (plan §6): stay on **Python + Flask** with server-rendered UI
through Phase 4, keep a clean JSON API so a different front end can be added
later, **Foundry** for contracts, a Python **FastMCP** server.

## What exists today (after commits 1–7)

### Layout

```
agents-list/
├── app/                      Flask package
│   ├── __init__.py           create_app(): config, extensions, blueprints, CLI, migrations
│   ├── catalog/              /, /marketplace, /agent/<id>, /checkout/<id>, /order/<id>, jobs pages
│   ├── seller/               /seller/* listing wizard, dashboard, orders, verification, manage
│   ├── admin/                /admin/* pages and API-key protected actions
│   ├── api/                  /api/* JSON: agents, search, ratings, orders, disputes, tx feed, health
│   ├── chain/                /config.js, /api/x402/{domain,pay}, /api/onchain/info, legacy reads
│   ├── models/               SQLAlchemy models + migrations/ (Alembic via Flask-Migrate)
│   ├── services.py           shared query, order and stats helpers
│   ├── config.py extensions.py auth.py llm.py sample_data.py
│   ├── templates/ static/    Jinja templates, CSS design system, wallet/payment JS
├── chain/                    no Flask dependency
│   ├── config.py             chain id, RPC, explorer, contract registry, explorer_url()
│   ├── client.py             web3 client: EIP-1559 txs, signer reserve, ContractNotConfigured
│   ├── usdc.py               USDC EIP-712 domain discovery (+ fallback), signature recovery
│   ├── x402.py               x402 challenge/decorator (homegrown format, to be replaced)
│   └── erc8004.py            read-only ERC-721 identity client for the canonical registry
├── tests/                    pytest: smoke, validation, admin, config, chain, USDC, migrations
├── docs/                     ROADMAP, ARCHITECTURE, plans/morph-plan.md
├── Dockerfile docker-compose.yml wsgi.py requirements*.txt
└── NOTICE LICENSE
```

Planned additions (not present yet): `app/engagements/`, `contracts/`,
`services/scoping/`, `services/orchestrator/`, `harness/`, `manifests/`,
`vendors/`, `mcp/`.

### Data model

| Model | Purpose | Future |
|---|---|---|
| `Agent` | Catalog listing: category, billing, prices, model, seller wallet, tags, capabilities | Trimmed; public fields move to the ERC-8004 registration file, private config to the manifest |
| `Order` | A hire: buyer, amount, status (`pending_payment` → `in_escrow` → `in_progress` → `completed`) | Becomes `Engagement` with `Milestone` and `Scope` (commit 10) |
| `VerificationEntry` | Seller verification queue | Vendor attestation flow |
| `ModerationReport` | Disputes and reports | Linked to escrow disputes and the arbiter |
| `Payout` | Admin payout ledger (DB only) | Driven by escrow releases |
| `Review` | Buyer ratings | Mirrored to the ERC-8004 ReputationRegistry |
| `ChainTransaction` | Audit log of submitted txs, with explorer links | Fed by an event indexer |

Schema changes go through Alembic (`flask --app wsgi db migrate/upgrade`).
Development boots run `upgrade` automatically (`AUTO_MIGRATE`); production and
the Docker image run it explicitly before gunicorn.

### Chain layer

- **Network:** Ethereum Sepolia by default (chain id 11155111), every value
  overridable by env and read at call time. No network access at import or boot.
- **Payment token:** Circle USDC on Sepolia (`0x1c7D…7238`), EIP-3009.
- **Payment flow today:** the browser fetches `/api/x402/domain` (EIP-712
  domain read from the token's `name()`/`version()`, cached, falling back to
  `USDC`/`2`), the buyer signs `TransferWithAuthorization` to the platform
  treasury (`PAYMENT_RECIPIENT`), and `/api/x402/pay` verifies the signature
  and has the facilitator submit it with EIP-1559 fees. Without a facilitator
  key the order is recorded as `pending_payment`. This is a direct transfer,
  **not escrow**; `EngagementEscrow` with `receiveWithAuthorization` replaces it.
- **Identity/reputation:** canonical ERC-8004 registries are configured; only a
  read-only identity client exists until commit 9.
- **Legacy contracts** from the prototype (registry, reputation, staking, session
  escrow) have no Sepolia deployment; routes that need them return
  `503 NOT_DEPLOYED`.

### Degradation rules

The app must boot on a fresh checkout with no env vars and no network:

- SQLite in `instance/` and auto-migrations for development.
- Chain reads fall back (domain) or return 503 (legacy contracts); writes need
  explicit signer keys and a configured recipient.
- The optional LLM preview is disabled without `LLM_URL`.
- The test suite pins `RPC_URL` to an unroutable port so it never touches the
  network.

## Security notes

- Separate keys per role (facilitator, gatekeeper; later arbiter and deployer);
  no shared fallback key. Signers keep an ETH reserve floor.
- Admin mutations require `X-Api-Key` when `API_KEY` is set; production config
  validation refuses to start with default secrets, SQLite, wildcard CORS or no
  API key (unless `STRICT_PROD_VALIDATION=0`).
- Payment tools in the future MCP server prepare transactions; the human signs.
- VMs will hold client credentials and a policy-limited wallet key: per-engagement
  secrets, egress controls, spend caps, a kill switch and audit logs (plan §8.5).
