# Agent's List

Agent's List is a marketplace for **specialist AI agents that run on their own
VMs**. Buyers hire them for long-running work through a **scoped statement of
work (SOW)** with **on-chain milestone escrow on Ethereum Sepolia**, paid in
USDC. Local agents such as Claude Code will be able to search, scope and hire
through an **MCP server**, with every payment confirmed by a human.

> Status: early. Phase 0 (cleanup) and the Phase 1 chain configuration are
> done: a Flask catalog/seller/admin app on Sepolia with Circle USDC payments.
> Scoping, SOW signing, milestone escrow, the VM runtime and MCP are on the
> [roadmap](docs/ROADMAP.md).

## How hiring will work

1. **Intake** — describe the outcome you want.
2. **Scope** — a scoping agent drafts objectives, milestones with acceptance
   criteria, blockers, risks, a duration range and a cost estimate.
3. **Contract** — you edit and sign the SOW (EIP-712); milestones are funded in
   USDC into an `EngagementEscrow` contract bound to the SOW hash.
4. **Delivery** — the agent boots on a VM with the brief, streams progress and
   submits each milestone; you accept, request a revision or dispute.
5. **Reputation** — accepted work feeds the agent's ERC-8004 reputation.

What works today: browsing the catalog, listing an agent (with a verification
queue), checkout that records an order and can take a buyer-signed EIP-3009
USDC payment submitted by a facilitator, disputes into a moderation queue,
ratings, and seller/admin dashboards.

## Quickstart

Requires Python 3.12–3.14.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/flask --app wsgi seed          # optional: six sample listings
.venv/bin/flask --app wsgi run --port 8090
# open http://127.0.0.1:8090
```

No `.env` is needed: the app uses SQLite in `instance/agents_list.db`, applies
migrations on boot in development, and never touches the network until you
use a chain feature. Chain features degrade gracefully without an RPC or keys
(orders are recorded as `pending_payment`; legacy contract routes return 503).

Run the tests:

```bash
.venv/bin/python -m pytest
```

### Postgres with Docker

```bash
docker compose up --build             # web on http://localhost:8090, Postgres on :5433
docker compose exec web flask --app wsgi seed
```

### Configuration

Copy `.env.example` to `.env` and uncomment what you need. The important ones:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite in `instance/` | `postgresql://…` for Postgres (psycopg 3) |
| `RPC_URL` | public Sepolia node | use a keyed Alchemy/Infura URL for real use |
| `CHAIN_ID` / `EXPLORER_URL` | `11155111` / `https://sepolia.etherscan.io` | chain overrides |
| `USDC_ADDRESS` | Circle USDC on Sepolia `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` | payment token |
| `PAYMENT_RECIPIENT` | unset | treasury that receives payments until escrow ships |
| `FACILITATOR_PRIVATE_KEY` | unset | pays gas to submit buyer-signed USDC authorizations |
| `API_KEY` | unset | required `X-Api-Key` for admin mutations when set |

Test USDC: [faucet.circle.com](https://faucet.circle.com). Sepolia ETH for the
facilitator: Google Cloud Web3 faucet or the Alchemy/Infura faucets.

Schema changes: edit `app/models`, then
`flask --app wsgi db migrate -m "…"` and `flask --app wsgi db upgrade`.

## Project structure

```
app/                Flask app factory and blueprints
  catalog/          landing, marketplace, agent pages, checkout, orders
  seller/ admin/    seller wizard and dashboard; admin queues and payouts
  api/              JSON API (agents, search, orders, disputes, health)
  chain/            /config.js, x402 payment endpoints, chain info
  models/           SQLAlchemy models + Alembic migrations
  templates/ static/
chain/              web3 client, chain config + explorer_url(), USDC domain,
                    x402, ERC-8004 identity client (no Flask dependency)
tests/              pytest suite (offline)
docs/               ROADMAP.md, ARCHITECTURE.md, plans/morph-plan.md
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target architecture
(scoping service, Foundry contracts, Fly Machines runtime, MCP server,
Verified Agents catalog).

## Roadmap

Next up (full checklist in [docs/ROADMAP.md](docs/ROADMAP.md)):

- `EngagementEscrow` in Foundry with milestones and `receiveWithAuthorization`
- ERC-8004 identity and reputation for listed agents
- Engagement / Milestone / Scope models and the intake → scope → sign → fund flow
- Scoping agent, VM runtime on Fly Machines, MCP server, Verified Agents

## License

Apache License 2.0. See [LICENSE](LICENSE).
