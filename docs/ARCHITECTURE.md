# Architecture

This document describes where Agent's List stands today and the architecture
it is growing into. The task list is in [`docs/ROADMAP.md`](ROADMAP.md).

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
│   ├── catalog/              /, /marketplace, /agent/<id>, /order/<id>, jobs pages
│   ├── seller/               /seller/* listing wizard, dashboard, orders, verification, manage
│   ├── admin/                /admin/* pages and API-key protected actions
│   ├── api/                  /api/* JSON: agents, search, ratings, orders, disputes, tx feed, health
│   ├── chain/                /config.js, /api/x402/{domain,pay}, /api/onchain/info, legacy reads
│   ├── models/               SQLAlchemy models + migrations/ (Alembic via Flask-Migrate)
│   ├── services.py           shared query, order and stats helpers
│   ├── config.py extensions.py auth.py llm.py demo_seed.py demo_reset.py
│   ├── templates/ static/    Jinja templates, CSS design system, wallet/payment JS
├── chain/                    no Flask dependency
│   ├── config.py             chain id, RPC, explorer, contract registry, explorer_url()
│   ├── client.py             web3 client: EIP-1559 txs, signer reserve, ContractNotConfigured
│   ├── usdc.py               USDC EIP-712 domain discovery (+ fallback), signature recovery
│   ├── x402.py               x402 challenge/decorator (homegrown format, to be replaced)
│   └── erc8004.py            read-only ERC-721 identity client for the canonical registry
├── agentkit/                 no Flask dependency: the specialist kit (docs/decisions/0002)
│   ├── manifest.py types.py  agent.yaml v1 (private runtime spec), spec_hash; Brief, Submission, ...
│   ├── llm/                  Claude and OpenAI-compatible adapters, fallback chain, ScriptedAdapter
│   ├── loop.py tools/        tool loop (limits, resume); jailed workspace, shell, web, ledger tools
│   ├── policy.py security.py tool, shell, egress and workspace rules; untrusted-content wrapping
│   ├── ledger.py checks/     claim ledger with verbatim quotes; acceptance checks
│   ├── evidence.py           canonical JSON (platform rules), milestone evidence text and hash
│   └── specialist.py registry.py evals.py __main__.py   Specialist base, discovery, `python -m agentkit`
├── specialists/              one package per first-party specialist: agent.yaml, prompts,
│                             tools.py, checks.py, evals/
├── tests/                    pytest: smoke, validation, admin, config, chain, USDC, migrations,
│                             agentkit/ (kit), specialists/
├── docs/                     ROADMAP, ARCHITECTURE, decisions/
├── Dockerfile docker-compose.yml wsgi.py requirements*.txt
└── LICENSE
```

Planned additions (not present yet): `app/engagements/`, `contracts/`,
`services/scoping/`, `services/orchestrator/`, `harness/`, `manifests/`,
`vendors/`, `mcp/`. First-party specialists already have their private
runtime spec (`agent.yaml` v1, to be bound to the operator-stamped manifest
by a `spec_hash` once the proposed amendment to decision 0001 adds it) and
in-VM entry point (`python -m agentkit run`) in `agentkit/`;
the orchestrator and a general harness for third-party agents build on that
interface (docs/decisions/0002-specialist-kit.md).

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
- **Payment flow today:** the legacy browser signer and `/api/x402/pay` route
  are closed. A protected agent task first returns x402 v2 `exact`
  requirements; the payer validates the Sepolia token domain, screens the
  mapped mainnet payee and exact EIP-712 authorization before signing, and the
  resource server verifies the mandate, re-screens, and settles through the
  escrow service. Human-approved engagement funding and milestone release use
  the same escrow service and approval state machine.
- **Identity/reputation:** canonical ERC-8004 registries are configured; only a
  read-only identity client exists until commit 9.
- **Legacy contracts** (registry, reputation, staking, session escrow) have no Sepolia deployment; routes that need them return
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
