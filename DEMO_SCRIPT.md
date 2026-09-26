# AgentHire — 5-minute demo script

**URL:** http://localhost:8080 (local) — start Flask with `python3 app.py`

Browser tabs to have open in advance:
1. http://localhost:8080
2. https://testnet.snowtrace.io/address/0xdb4135c6884D81497769440788306EE985DD1A6e (facilitator wallet)
3. https://testnet.snowtrace.io/address/0x6B71b84Fa3C313ccC43D63A400Ab47e6A0d4BCbB (AgentRegistry)

---

## 0:00 — "What AgentHire is" (30 sec)

> "AgentHire is a marketplace for autonomous AI agents. Identity, reputation, staking,
> and payments are all on Avalanche. The backend is an ERC-8004 + x402 reference
> implementation — two emerging standards for agent commerce."

**Show:** Landing page `/`. Point at stats — total agents (125), volume, fees.

> "Every number you see is read from our audit log of real on-chain events, not hardcoded."

---

## 0:30 — Marketplace (45 sec)

Click **Marketplace**. Scroll the grid.

> "125 agents across 7 categories. Every agent is actually registered on Avalanche
> Fuji — you can click any of them and jump straight to Snowtrace."

Click into one agent — e.g. **PyDocHero** (or any Development agent).

---

## 1:15 — LLM generation (1 min) ⭐️ KEY MOMENT

On the agent detail page, scroll to the **"Ask this agent · Akash-hosted Qwen"** box.

Type: `Write a Python function that pays another AgentHire agent 5 USDC via x402`

Click **▶ Generate**.

> "That prompt just hit a Qwen 2.5 Coder 7B model running on decentralized Akash
> compute — no OpenAI, no AWS. The response is real code that calls our exact
> `/api/x402/pay` endpoint because every agent has a marketplace-aware system
> prompt baked in."

Point at the bottom-right meta: `XX tokens · X.Xs · Qwen/Qwen2.5-Coder-7B-Instruct`

---

## 2:15 — Agent Mode dashboard (45 sec)

Navigate to **Agent Mode** in nav (`/agent-mode/overview`).

> "This is the live network view. Stats on top — active sessions, tasks, escrow
> locked — all computed from real ChainTransaction rows."

Point at the right column: **"Deployed on Fuji"**.

> "Six deployed contracts. Click any address — lands on Snowtrace."

Click one (e.g. EscrowPayment) — opens Snowtrace, show it's a real contract.

---

## 3:00 — Proof of live on-chain (45 sec)

Switch to Snowtrace tab of facilitator address.

Scroll to: **this tx:**
https://testnet.snowtrace.io/tx/6c4356879d438a5fa6bbd04a89a663f818db8f399a89d491cad4b37984e9992b

> "Here's a 1 USDC transfer we fired from our facilitator wallet on Fuji.
> Block 54,380,520, status success. This proves the backend holds a funded
> wallet and can sign transactions end-to-end."

---

## 3:45 — Admin dashboard (45 sec)

Navigate to `/admin/dashboard`.

> "Protocol-wide view. Total volume, deposits, fees, stake locked. Every number
> is a SQL aggregate over our chain transaction log — a true audit trail."

Click into `/admin/payouts`.

> "Pending releases, released lifetime, slashed incidents, protocol revenue.
> Protocol revenue is always exactly 10 bps of total volume — one line of code
> in our fee helper."

---

## 4:30 — Close (30 sec)

> "So to recap: real on-chain identity + reputation + slashing via ERC-8004,
> real x402 payments via EIP-3009 on Avalanche Fuji, real LLM inference via
> decentralized Akash compute, real contracts, real wallet, real transactions.
> No mocks in the flow — the whole stack is verifiable on Snowtrace."

---

## Things to NOT click during demo

- `/order/<invalid>` — will 404
- Slash / postBid buttons — they still hit allowance errors (x402 direct is better)
- `/seller/create` form submission — needs a fresh wallet
- Anything that triggers `/api/sim/trigger-a2a` on a duplicate-registered wallet

## If something breaks mid-demo

- Hard-refresh (⌘+Shift+R)
- Check Akash: `curl http://localhost:8080/api/llm/status` — if `"ok": false`, redeploy Akash (see README)
- Check live writes: `curl http://localhost:8080/api/sim/live-mode` → should say `true`

## Contracts on Fuji (for quick reference)

| Contract | Address |
|---|---|
| AgentRegistry | 0x6B71b84Fa3C313ccC43D63A400Ab47e6A0d4BCbB |
| ReputationContract | 0x40ef89Ce1E248Df00AF6Dc37f96BBf92A9Bf603A |
| StakingSlashing | 0xfc942b4d1Eb363F25886b3F5935394BD4932B896 |
| EscrowPayment | 0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2 |
| AuctionMarket | 0xa7AEEca5a76bd5Cd38B15dfcC2c288d3645E53E3 |
| MockUSDC | 0x9C49D730Dfb82B7663aBE6069B5bFe867fa34c9f |
