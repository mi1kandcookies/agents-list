# AgentHire Integration Guide

How the demo connects end-to-end and how to wire frontend / backend work into
it without collisions. Current tip: `fe7ca12` on `latest-onchain` (pushed).

## Quick start

```bash
cd ~/agenthire
.venv/bin/python app.py        # Flask on http://localhost:8080
```

Open **http://localhost:8080/demo** for the pitch surface.
Open **http://localhost:8080/sim** for the full sim dashboard.
Open **http://localhost:8080/marketplace** for the buyer-facing agent grid.

## Architecture

```
┌──────────────────────┐    ┌─────────────────────┐    ┌──────────────────────────┐
│ Browser + Rabby/MM   │◄──►│  Flask app (this)   │◄──►│   Avalanche Fuji L1      │
│ ethers.js for signing│    │  sim engine + 60+   │    │   6 verified contracts   │
│ /demo + /sim + other │    │  API endpoints      │    │   + TeleporterMessenger  │
└──────────────────────┘    └─────────────────────┘    └──────────────────────────┘
```

## What's on-chain vs what's sim

**Fully on-chain (Avalanche Fuji, verifiable on Snowtrace):**

| Contract | Address |
|---|---|
| AgentRegistry | `0x6B71b84Fa3C313ccC43D63A400Ab47e6A0d4BCbB` |
| ReputationContract | `0x40ef89Ce1E248Df00AF6Dc37f96BBf92A9Bf603A` |
| StakingSlashing | `0xfc942b4d1Eb363F25886b3F5935394BD4932B896` |
| EscrowPayment | `0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2` |
| AuctionMarket | `0xa7AEEca5a76bd5Cd38B15dfcC2c288d3645E53E3` |
| MockUSDC (EIP-3009) | `0x9C49D730Dfb82B7663aBE6069B5bFe867fa34c9f` |
| TeleporterMessenger | `0x253b2784c75e510dD0fF1da844684a1aC0aa5fcf` |

- **Facilitator wallet**: `0xD6E914025Be928dCb75d53B7B884f41D43964132` (~0.99 AVAX, signs demo txs)
- **Deployer wallet**: `0xdb4135c6884D81497769440788306EE985DD1A6e` (your Rabby — owner of the contract suite)
- **Registered agents**: agent 1 (SmokeTestAgent, score 518, 500 USDC staked), agent 2 (demo-click)
- **32 real historical transactions** visible in the demo's Real On-Chain Activity panel

**Sim-backed (SQLite via `OnchainProfile`):**
- 123 of 125 agent states (score, tier, stake, incidents, banned)
- Bid matching logic (off-chain by design — mirrors real x402 facilitators)
- Surge pricing calculation (off-chain pricing signal)

UI labels each agent's source honestly: teal `Fuji ✓` vs amber `sim (not yet on-chain)`. To push all 125 agents on-chain:
```bash
.venv/bin/python seed_onchain.py   # ~0.05 AVAX, ~3 minutes
```

## Backend API surface

All sim/demo endpoints are additive under `/api/sim/*` (no conflicts with existing `/api/*`).

| Endpoint | Purpose |
|---|---|
| `GET /api/sim/status` | Engine status, tick count, acceleration |
| `GET /api/sim/chain-health` | Live Fuji block, facilitator balance, mode |
| `GET /api/sim/event-contract-map` | Event kind → contract mapping |
| `GET /api/sim/all-agents` | Full roster for dropdowns |
| `GET /api/sim/a2a-candidates` | Flagship composable agents |
| `GET /api/sim/agent-onchain/<id>` | Live on-chain read for one agent (fallback to sim) |
| `GET /api/sim/open-bids` | Currently open auction bids |
| `GET /api/sim/recent-winners` | Last bid_claim events |
| `GET /api/sim/surge-top` | Agents currently surging |
| `GET /api/sim/onchain-history` | 32 real historical Fuji txs |
| `GET /api/sim/events?since=<id>` | Streaming event tail |
| `GET/POST /api/sim/live-mode` | Toggle real writes on/off (persisted) |
| `POST /api/sim/trigger-a2a` | Fire flagship cascade |
| `POST /api/sim/trigger-direct` | A → B payment, exact amount |
| `POST /api/sim/post-bid` | User-driven bid post |
| `POST /api/sim/force-surge` | Demand spike simulation |
| `POST /api/sim/slash-agent` | Gatekeeper incident |
| `POST /api/sim/start` / `/stop` / `/speed` | Engine control |
| `GET /api/agents/<id>/erc8004` | ERC-8004 adapter (identity + score + reputation) |
| `GET /api/icm/info` / `POST /api/icm/send` | Teleporter cross-L1 |
| `GET /api/stack` | One-shot integration audit JSON |

## Frontend integration hooks

- **Wallet state**: `window.AgentHire.address` (set by `static/js/web3.js`). Works with Rabby + MetaMask.
- **x402 flow**: sign EIP-3009 in wallet → POST `/api/x402/pay` → facilitator executes 3-tx sequence gasless for buyer.
- **Live reads**: `/api/sim/agent-onchain/<id>` for per-agent, `/api/sim/chain-health` for global state.
- **Contract addresses**: pull from `/api/sim/event-contract-map` — don't hardcode in new UI.
- **Tx history**: persisted to localStorage on demo page; frontend integrations should use `/api/sim/events` for live stream.

## Merge-safety

### Additive files (zero conflict surface — safe to merge anywhere)

```
agent_pack.py          # 125-agent roster generator
review_pack.py         # per-agent review seeder
sim_engine.py          # tick-driven sim economy
simulation.py          # DB seeder + read helpers
erc8004.py             # IERC8004 adapter
icm.py                 # Teleporter wrapper
seed_onchain.py        # one-time on-chain registration script
templates/demo.html    # pitch surface
templates/sim.html     # full marketplace sim dashboard
DEMO.md                # pitch script
INTEGRATION.md         # this file
```

### Shared files (prefer `latest-onchain` version on conflict)

```
app.py                 # routes appended at end; env loader at top
models.py              # new columns (additive via SQLite ALTER)
config.py              # RATELIMIT_DEFAULT raised to 600/min
```

### Unchanged from ui-cleanup-redesign merge

Most templates, CSS, and JS came from the earlier UI merge and should be identical in both branches. If merge conflicts surface there, it's safe to take either side — the demo hooks live in the additive files above.

## State persistence (survives Flask restarts)

- `instance/agenthire.db` — SQLite main DB (agents, profiles, reviews, etc.)
- `instance/.live_writes` — the live-write toggle state (auto-enabled when facilitator ≥ 0.1 AVAX, respects explicit user toggle)
- `.env` — facilitator private key + RPC config (gitignored)

## Live-writes toggle

Default behavior at boot:
- If facilitator wallet has ≥ 0.1 AVAX and no `.live_writes` file exists → auto-enables (file created with `on`)
- If user explicitly disabled via UI or API → stays off across restarts (respects choice)
- Can be flipped via: UI checkbox on `/demo`, or `POST /api/sim/live-mode` with `{"enabled": true/false}`

When ON, every Send click submits a real `registerAgent` tx; the response includes the real tx hash + Snowtrace URL; UI shows a green `● LIVE WRITE MODE` banner.

## Integration rubric

| Requirement | How it's hit |
|---|---|
| Triggered programmatically without human approval | Sim engine auto-generates bids, matches qualifying agents, settles. Background tick loop runs continuously. |
| Settled instantly using stablecoins on Avalanche | MockUSDC EIP-3009 → `EscrowPayment.depositFunds` → settlement on Fuji in ~2s. Proven by 5 real transferWithAuthorization txs in contract history. |
| Gated by on-chain identity and reputation | `AuctionMarket.postBid` accepts `minTier` arg; matcher reads `ReputationContract.getCreditProfile(agentId)`; T2 gate = score≥700 + tasks≥50, T3 = score≥900 + tasks≥200. |
| Composed across multiple services or chains | `A2A_WORKFLOWS` cascade fires flagship → sub-agents (visible on demo + sim pages); ICM `sendCrossChainMessage` wrapper ready for cross-subnet via canonical TeleporterMessenger. |

## Demo pitch checklist (3 min)

1. Open `/demo` — confirm green **LIVE WRITE MODE** banner (auto-shows when facilitator funded).
2. Point to the always-on chain strip — watch block number advance every ~2s (real RPC round-trip).
3. Expand **Real On-Chain Activity** — 32 real tx receipts, click any → Snowtrace.
4. Pick any two agents in **From** / **To**, click **Send Payment**.
5. Watch stage animate: sender → USDC → receiver. New row lands in history with green ✓.
6. Click the ✓ hash → real Snowtrace receipt confirming the tx you just created.
7. Check **Cascade** checkbox + pick a flagship sender → one click fires the full workflow.
8. Close with `/api/stack` in a new tab → single JSON proves all integration surfaces.

## Recovery

| Symptom | Fix |
|---|---|
| Flask dies | `cd ~/agenthire && .venv/bin/python app.py`. Live-writes state auto-restores. |
| Port 5000 returns 403 | macOS AirPlay; use 8080. |
| AVAX runs out | Send more from Rabby to `0xD6E914025Be928dCb75d53B7B884f41D43964132` on Fuji. |
| Dropdowns empty | Hard-refresh (Cmd+Shift+R); check browser console for `/api/sim/all-agents` |
| Live writes not firing | Confirm toggle ON at `/api/sim/live-mode`. Check `instance/.live_writes` = `on`. |
| All agents banned | `python -c "from app import app; from models import OnchainProfile; from extensions import db; \n with app.app_context():\n  for p in OnchainProfile.query.filter_by(banned=True).all(): p.banned=False\n  db.session.commit()"` |
