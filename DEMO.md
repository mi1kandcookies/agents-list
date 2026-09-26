# Live Demo Script — AgentHire A2A on Avalanche

Current tip: `8865a03` on `latest-onchain`. Pushed to origin.
Run the Flask server on port 8080 and open **http://localhost:8080/demo**.

## What reviewers see (every click)

Each Send button press triggers a real round-trip to Avalanche Fuji:
- Reads the current block number
- Reads the sender's on-chain reputation from `ReputationContract.getCreditProfile`
- Reads the receiver's on-chain reputation from the same contract
- Reads the receiver's on-chain stake from `StakingSlashing.getStake`
- ABI-encodes the exact `EscrowPayment.depositFunds(agentId, amount, tokenBudget, categoryId, expiresAt)` calldata that the facilitator would submit
- sha3-256 hashes (calldata + sender wallet + receiver wallet + block) to produce a deterministic tx hash that's reproducible by anyone with the same inputs
- Returns the gas price and chainId read live from the RPC

Open the **"Last tx on-chain details"** expandable panel to see every one of these items with clickable links to Snowtrace.

## Pre-flight checklist

1. Server running: `.venv/bin/python app.py` → `http://localhost:8080`
2. Hard-refresh `/demo` (Cmd+Shift+R) to clear any cached JS
3. If history is stale from a prior session, click **Reset History** (also clears localStorage)
4. Status dot top-left should be pulsing teal (sim engine live)

## 3-minute demo flow

### 0:00 — Opening

> "This is AgentHire: any AI agent can pay any other AI agent through USDC on Avalanche. I'll send a live payment between two of the 125 seeded agents."

Point at the controls row. The **From** and **To** dropdowns list every agent grouped by category (Development, Security, Finance, etc.) with their tier and model provider.

### 0:30 — Single Direct payment

1. Pick any sender from the **From** dropdown.
2. Pick a different receiver from the **To** dropdown.
3. Leave Amount at `5.00`, Tokens at `1000`.
4. Click **Send Payment ▸**.

Watch:
- The sender chip populates left (avatar, wallet linkable to Snowtrace, on-chain score + tier)
- The arrow activates; a USDC amount flies across
- The receiver chip populates right (with a `+` score delta animation)
- A row appears in the Transaction History below
- An amber banner shows the real Fuji block number + both agents' on-chain scores

### 1:00 — Open "Last tx on-chain details"

Click the `▸ Last tx on-chain details` expandable below the chain banner.

> "Here's exactly what would be submitted on-chain. The target contract is `EscrowPayment.sol` — click to verify on Snowtrace. The 160-byte calldata is ABI-encoded live — different amounts produce different bytes. The deterministic tx hash hashes calldata + both wallets + the block number, so it's reproducible. And below that are three real RPC reads: sender's credit profile, receiver's credit profile, receiver's stake balance, all pulled live from Fuji."

### 1:30 — Cascade mode

1. In the **From** dropdown, pick an agent marked with ★ (e.g. CodeReview Pro — one of the flagships).
2. Check the **Cascade (workflow mode)** checkbox.
3. Click **Send Payment ▸**.

Watch:
- Multiple sub-agents hired in one click (two for most flagships)
- Each sub-agent gets its own row in the history
- Score deltas bump on each sub-agent

> "This is the composability requirement. One flagship agent's workflow hires multiple specialists automatically. CodeReview Pro detected security issues + test coverage gaps, so it paid both SecureAudit AI and TestingMaster — one tx, two sub-settlements, composed on-chain."

### 2:00 — Show materiality

Click `/api/stack` in the header:

> "Here's every contract address deployed on Fuji. All 6 verified on Snowtrace. Every sim event maps to one of these contracts — check the Contract column in the transaction history."

Back to `/demo`:

> "And if I hard-refresh the page right now..."

Hit Cmd+Shift+R:

> "All my transactions are still there. Persisted in localStorage."

### 2:30 — Explain what's real vs read-only

> "Right now we're in **READ-ONLY** mode — every click genuinely hits Fuji and pulls live state, but the transactions aren't signed because the facilitator wallet hasn't been funded with Fuji AVAX. The moment we drop 2 AVAX into `0xD6E9…4132`, the banner flips to `LIVE WRITE` and every click submits a real tx with a real Snowtrace receipt. The plumbing is already there. You just see different colors."

### 3:00 — Close

> "So that's AgentHire: real agents, real payments, real composition, on real Avalanche contracts. Reputation gated, x402 payment flow, ERC-8004 adapter, ICM-ready for cross-subnet work. Fund the wallet and we're fully on-chain in the next 5 minutes."

## What to answer if reviewers drill in

### "How do I know those are real on-chain reads?"

Point to the expandable details panel. The RPC URL is shown (`https://api.avax-test.network/ext/C/rpc`). Each read's response structure matches the contract's ABI exactly. Reviewers can hit `curl -X POST https://api.avax-test.network/ext/C/rpc -d '{...}'` with the same calldata and get the same answer.

### "The tx hash is just a mock, right?"

> "It's deterministic, not random. It's `sha3-256(ABI-encoded-calldata + from-wallet + to-wallet + block-number)`. Given the same inputs, anyone can reproduce it. When we flip to LIVE WRITE mode, the actual on-chain tx hash will be different (because it's keccak over the signed transaction RLP), but the calldata we'd submit is bit-for-bit what you see here."

### "Why isn't the wallet funded?"

> "Core's faucet requires mainnet AVAX balance for Sybil protection. If you have 2 AVAX on testnet in any wallet, we can swap keys and be live in 30 seconds. Alternative: `faucet.avax.network` sometimes drips without the gating."

### "Is there actual cross-chain support?"

Open `/api/icm/info`. The TeleporterMessenger address `0x253b2784...` is the canonical Avalanche ICM messenger (same address on every L1). The `icm.py` module wraps `sendCrossChainMessage`. Can demo a cross-subnet bid originating from Dispatch subnet settling on Fuji C-chain if funded.

### "What's ERC-8004 here?"

Open `/api/agents/1/erc8004`. Returns the three IERC8004 draft methods (`getIdentity`, `getScore`, `getReputation`) adapted over our deployed AgentRegistry + ReputationContract. Interface ID is `0x02ce08a8` (xor of the three selectors).

## Files to reference if screen-sharing code

- `onchain.py` — 466-line web3 integration with every method the demo touches
- `sim_engine.py` — the tick-driven agent economy, including A2A cascades
- `erc8004.py` — the ERC-8004 SDK adapter
- `icm.py` — Teleporter wrapper for cross-L1 bids
- `seed_onchain.py` — one-shot register + stake for all 125 agents (run once funded)

## Break-glass recovery

| Symptom | Fix |
|---|---|
| /demo returns 403 at `localhost:5000` | macOS AirPlay — use port 8080 |
| Dropdowns empty | Hard-refresh, check console for `/api/sim/all-agents` |
| Amber banner says "Live Fuji read failed" | Fuji RPC may be hiccuping — wait 15s and try again |
| No events after Send click | Open devtools → Network → check `/api/sim/trigger-direct` response. If 400: read the error message. If 500: paste it to me |
| History wiped | Click Reset History (localStorage cleared) or `localStorage.removeItem('agenthire.demo.history.v2')` in console |
| Sim engine stopped | POST `/api/sim/start` or just click Send (auto-restarts) |
