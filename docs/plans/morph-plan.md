# Agent's List: plan for morphing `agenthire` into `agents-list`

Planning doc, written 2026-09-26. Source: read-only review of `/Users/danielzilper/projects/agenthire` (tip `ab317f2`, remote `shalpate/agenthire`). No code was changed.
User decisions this plan assumes: stay on-chain, move from Avalanche Fuji to **Ethereum Sepolia**, and leave out trading strategies.

---

## 1. Thesis

agenthire is a prototype **storefront for per-call AI agents**. It has a marketplace catalog, agent detail pages, a checkout that gets an EIP-3009 USDC permit signed through a hand-rolled x402 402-challenge, seller onboarding and verification tiers, admin moderation and payouts, and on-chain identity, reputation and staking reads against six Fuji contracts. Agent's List needs a **professional-services engagement platform for long-running agents**: intake → scoping agent → SOW/quote → signed contract with milestone escrow → an agent booted on a VM with the brief → milestone delivery and acceptance → reputation. An MCP server and a Verified-Agents catalog sit on top.

Three things carry over: the marketplace/seller/admin UI skeleton, the Python chain layer patterns, and the x402/EIP-3009 know-how. **The core of Agent's List is missing:** scoping, SOW, real escrow, milestones, VM runtime, agent manifests, and MCP. The "escrow" that exists is not real escrow. `OnChain.x402_execute` (`onchain.py:419-456`) sends USDC straight to the recipient, the hire path never calls `EscrowPayment.depositFunds`, `settleSession` is not even in the ABI, and `/api/orders/<id>/complete` (`app.py:2899-2916`) only flips a DB status while telling the user "Escrow released". The "125 agents" are synthetic: `agent_pack.py` plus the sim engine, all answered by one Qwen prompt (`llm.py`). Treat agenthire as a **UI and pattern donor, not a foundation to extend in place.**

## 2. Concept mapping

| Idea-doc concept | agenthire today | Status |
|---|---|---|
| Marketplace, browse and search agents | `/marketplace`, `/agent/<id>`, `/api/agents`, `/api/search`; `Agent` model | **Adapt**: fields are per-token/per-minute and surge-oriented |
| Offer agent services (supply side) | `/seller/create` 4-step wizard, `/seller/agents/<id>`, `seller/*` templates | **Adapt**: listing becomes an *agent manifest* |
| Hire flow | `/checkout/<id>` → `/api/x402/pay` or `/api/x402/demo-execute/<id>` → `Order` | **Adapt heavily**: becomes intake → scope → contract |
| Role, responsibilities, SOW, timeline, success factors, milestones, outcomes | `Order.task` (a single free-text column) | **Missing** |
| Scoping agent (cost, feasibility, missing conditions, duration) | `/api/agents/<id>/generate` (Qwen chat with a pricing bio) | **Missing**: the only echo is the LLM call pattern |
| Contract signed | none | **Missing** |
| Escrow with milestones | `EscrowPayment` ABI (session-based, token budget); not used on the hire path | **Missing**: needs a new contract |
| Agent-to-agent payments | `/api/sim/trigger-direct`, `/api/sim/trigger-a2a` (sim cascades); x402 decorator (`x402.py:163`) | **Adapt**: keep x402 for per-call A2A, add subcontract escrow |
| Agent identity | `AgentRegistry` (custom) + `erc8004.py` adapter shim | **Replace** with canonical ERC-8004 registries on Sepolia |
| Reputation | `ReputationContract` + `simulation.py` scores | **Replace** with ERC-8004 ReputationRegistry + off-chain aggregation |
| Staking and slashing | `StakingSlashing`, `/api/seller/bond` | **Defer** (optional provider bond later) |
| Auctions | `AuctionMarket`, `/api/auctions/*` | **Drop** (the scoping/quote flow replaces it) |
| Specialist config (model, skills, persona, plugins, memory, MCPs, tools) | `Agent.model_provider/model_name`, `capabilities`, `tags` | **Missing**: needs a manifest spec |
| VMs on standby until hired | none (one shared Akash vLLM endpoint) | **Missing** |
| MCP server for local agents | none | **Missing** |
| Verified Agents (real vendors) | `verified` flag and `verification_tier` (basic $10 / thorough $50), admin queue, sandbox gates (mock) | **Adapt**: the concept fits, the data is mock |
| Trust and safety | `ModerationReport`, `/admin/moderation`, disputes → `submitIncident` | **Keep and adapt** |
| Payouts | `Payout` model, `/admin/payouts` (DB-only) | **Adapt**: payouts come from escrow release |

## 3. Inventory: KEEP / ADAPT / DROP / ADD

**KEEP (port nearly as-is)**
- `extensions.py`, `auth.py`, and `config.py` including `validate_runtime_config`. These are solid production guards; just rename the env defaults.
- `static/css/main.css` (the design system) and the `static/img/*` logo assets, until rebranding.
- `wsgi.py`, `Dockerfile`, `tests/` (the harness pattern; the assertions get rewritten).
- `.env.example` structure. Every address is already env-overridable (`onchain.py:68-81`, `/config.js` at `app.py:2122`), which makes the chain move mostly config.

**ADAPT**
- `onchain.py`: keep the class shape, the signer-reserve guard and the nonce sequencing. Retarget it to the new contracts, switch to EIP-1559 fees (it uses legacy `gasPrice` at `onchain.py:494`), and stop falling back to one `PRIVATE_KEY` for both facilitator and gatekeeper (`onchain.py:227-228`).
- `x402.py`: keep the decorator idea. Rewrite it to the current x402 spec (the `exact` scheme with CAIP-2 `eip155:11155111`) using the official Python SDK. Today's format (`x402/eip-3009` with a JSON `X-Payment`) is homegrown, so standard x402 clients won't interoperate with it.
- `erc8004.py`: rewrite as a thin client for the **real** ERC-8004 IdentityRegistry (ERC-721 + `agentURI` registration file) and ReputationRegistry. The current selectors (`getIdentity/getScore/getReputation`) are invented and do not match the finalized standard.
- `models.py`: keep `Agent` (trimmed), `Review`, `ModerationReport`, `Payout` and `ChainTransaction` (the audit log is useful). `Order` becomes `Engagement`. Drop the `OnchainProfile` sim fields, `PricePoint` and `AuctionBid`. Add Flask-Migrate/Alembic; today's `_ensure_columns` ALTER hack in `agent_pack.py:452` is not a migration strategy.
- `app.py` (4,253 lines): split into blueprints (`catalog`, `engagements`, `seller`, `admin`, `api`, `chain`). Delete the hardcoded `AGENTS` list (`app.py:342-674`) and the in-memory `AGENTS` mirror plus `_sync_agents_from_db` (`app.py:4069+`). Every view must read the DB.
- `llm.py`: keep the OpenAI-compatible client shape and delete the AgentHire/Fuji system prompt (`llm.py:19-28`). The scoping agent should run on a frontier model through the Anthropic API or Agent SDK, not Akash Qwen.
- Templates that survive, re-skinned: `base.html` (nav/wallet modal, with all Fuji code stripped), `marketplace.html`, `agent_detail.html`, `checkout.html` (becomes "Review scope and fund"), `order.html` (becomes the engagement page with milestones), `active_jobs.html`, `past_jobs.html`, `how_it_works.html`, `404/500.html`, `seller/{create,manage,orders,earnings,verification}.html`, `admin/{dashboard,verification_queue,review,moderation,payouts}.html`.
- `static/js/web3.js`, `contracts.js`, `main.js`: keep the wallet connect and EIP-712 signing and swap the ABIs. `tx-feed.js` needs an Etherscan-backed feed.
- `AGENT_DEPLOYER_PROMPT.md`: a useful template for "list your agent via your own coding agent". Rewrite it for the manifest and Sepolia.

**DROP**
- `sim_engine.py`, `simulation.py`, `review_pack.py` (fake reviews), the `agent_pack.py` roster generator (it is **not** a manifest; only `MODELS_POOL` and the category taxonomy are worth copying), `seed_onchain.py`, `icm.py`.
- The junk-agent filtering (`app.py:1161`, `1392`, `4214`), which becomes moot with a clean DB. Also the live-writes toggle (`app.py:3067-3121`), all `/api/sim/*` routes (`app.py:3025-4067`), `/api/icm/*`, `/api/stack` (integration audit), `/api/auctions/*`, `/api/price/*` and surge pricing, `/agent-mode*`, and `/api/x402/auto-sign` (the facilitator pays itself, demo-only).
- Templates: `demo.html`, `sim.html`, `agent_mode.html`, `admin/sandbox.html` (mock gates), the demo sections of `landing.html` (rebuild the landing page).
- Docs and scripts: `DEMO.md`, `DEMO_SCRIPT.md`, `INTEGRATION.md` (integration checklist), `run.bat`, the `scripts/*.ps1` demo/onchain-ready gates. Keep `scripts/fullstack_check.py` as a pattern only.
- `test_onchain_e2e.py`: replace it with Foundry tests plus a Sepolia smoke test.

**ADD**: see §5. Contracts (Foundry), scoping service, engagement/SOW domain, runtime orchestrator, agent manifest spec, MCP server, Verified-Agents catalog.

## 4. Avalanche Fuji → Ethereum Sepolia

**Contract sources are NOT in the repo.** No `.sol`, Hardhat or Foundry files exist on any branch (`main`, `latest-onchain`, `ui-cleanup-redesign`, `main-backup-*`). Only minimal ABIs and addresses are present (`onchain.py:111-206`, `static/js/contracts.js`, whose comment says "Full ABIs are in the backend repo"), and the friend's GitHub has no contracts repo. **All contracts must be rewritten and deployed to Sepolia.** That is acceptable because most of them get replaced anyway.

| Avalanche-specific item | Where | Sepolia action |
|---|---|---|
| Chain ID 43113 / `0xa869` | `onchain.py:50`, `x402.py:60,70`, `base.html:133,576`, `base.html:516` label, `.env.example` | `11155111` / `0xaa36a7`; derive every value from `/config.js` and delete the hardcoded `base.html` copies |
| RPC `api.avax-test.network/ext/C/rpc` | `onchain.py:49`, `config.py:63`, `.env.example`; **different path** `/ext/bc/C/rpc` in `icm.py:106`; `avalanche-fuji-c-chain-rpc.publicnode.com` hardcoded in `base.html:306,427,441,452,578` | Alchemy or Infura Sepolia (keyed) with a public fallback such as `ethereum-sepolia-rpc.publicnode.com` |
| Explorer: Snowtrace and `subnets-test.avax.network/c-chain` | `onchain.py:55,289,331-332,405,454-455,484`; `app.py:1038,1169,1443,1947-1949,2249,3156,3959`; `icm.py:164`; templates `agent_detail.html:496`, `checkout.html:333`, `order.html:214`, `past_jobs.html:17`, `landing.html:820,1099`, `admin/dashboard.html:262`, `seller/earnings.html:192,451` | `https://sepolia.etherscan.io`; add one `explorer_url(kind, id)` helper and rename every `snowtrace` JSON key to `explorer` |
| Snowtrace tx-list API | `app.py:3472` | Etherscan API v2 (`chainid=11155111`, API key required) or an indexer; for our own contracts, just read our event logs |
| AVAX native gas: `nativeCurrency AVAX`, reserve floor, balance gates, faucet link | `app.py:2136`, `onchain.py:87,510-519`, `app.py:3117,3148-3161,3217,3515`, `base.html:305,343,426,439-448`, `scripts/onchain-ready.ps1:3,45-50` | Sepolia ETH; rename `MIN_SIGNER_AVAX_RESERVE`→`MIN_SIGNER_ETH_RESERVE`. Faucets: Google Cloud Web3 faucet, Alchemy/Infura faucets (often need a mainnet balance). **Sepolia gas is spikier than Fuji**: budget more and use EIP-1559 |
| ~2s blocks ("Sub-2s settlement") | `how_it_works.html:135`, UI copy, `wait_for_transaction_receipt` defaults | ~12s blocks. Change the copy, go async with a "pending → confirmed" state, and never block a request thread on receipts |
| ICM/Teleporter `0x253b…5fcf`, blockchain IDs | `icm.py` (entire), `app.py:2240-2302` | **Delete.** No Sepolia equivalent is needed on a single chain. If cross-chain matters later, use CCTP (USDC) or CCIP |
| MockUSDC `0x9C49…4c9f` with EIP-712 domain `"Mock USDC"` v`"1"` | `onchain.py:58`, `x402.py:86,291`, `web3.js:206`, `seed_onchain.py` (`mint`) | **Circle USDC on Sepolia `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238`**. It natively supports EIP-3009 `transferWithAuthorization`/`receiveWithAuthorization`; its domain is name `"USDC"`, version `"2"`. Read `name()`/`version()` on boot instead of hardcoding. The Circle faucet (faucet.circle.com) is rate-limited, so deploy a mintable EIP-3009 MockUSDC **only** for CI and load tests behind `USDC_ADDRESS` |
| Six custom contracts (AgentRegistry `0x6B71…`, ReputationContract `0x40ef…`, StakingSlashing `0xfc94…`, EscrowPayment `0xD199…`, AuctionMarket `0xa7AE…`) | `onchain.py:57-64`; duplicated in `sim_engine.py:39-42`, `llm.py:24-28`, `simulation.py`, `seller/earnings.html:192`, `app.py:1949`, README/INTEGRATION | Identity and reputation go to the **canonical ERC-8004 registries already on Sepolia**: IdentityRegistry `0x8004A818BFB912233c491871b3d84c89A494BD9e`, ReputationRegistry `0x8004B663056A597Dffe9eCcC1965A193B7388713` (CC0, from `erc-8004/erc-8004-contracts`; the ValidationRegistry is not deployed there). Escrow becomes a new `EngagementEscrow.sol`. Staking is deferred. Auction is dropped |
| Facilitator/deployer wallets `0xD6E9…4132`, `0xdb41…1A6e` | README, INTEGRATION | New Sepolia keys; separate facilitator, arbiter and deployer keys |
| Gatekeeper sig binds `chainid` + contract | `onchain.py:465-473` | Works as-is on any chain, but it is superseded by the ERC-8004 feedback plus escrow-arbiter design |
| x402 facilitator network | self-hosted in `onchain.x402_execute` | **The hosted x402.org facilitator does not list Ethereum Sepolia** (it lists Base Sepolia among others). Self-host the facilitator (the official SDK supports any `eip155:*`), which agenthire effectively already does |
| "Avalanche" marketing copy | `base.html:120,623,762`, `landing.html:794,1105`, `how_it_works.html:116`, `checkout.html:24,49`, `active_jobs.html:10,87`, `past_jobs.html:10,79`, `admin/dashboard.html:26-71`, `agent_detail.html:236,398-419,691,723`, `sim_engine.py:771` | Rewrite (most of these pages are rebuilt anyway) |

## 5. New components

**5a. Contracts (Foundry, `contracts/`).** `EngagementEscrow.sol` is one contract with many engagements:
- `createEngagement(clientAddr, providerAgentId (ERC-8004), arbiter, token, milestones[{amount, dueAt, reviewWindow}], sowHash)`.
- Funding uses **`receiveWithAuthorization`**, not `transferWithAuthorization`, to avoid front-running. It can be gasless for the client via the facilitator, or funded by a plain `approve`.
- Per milestone: `submit(evidenceHash)` → `accept` (release) | `requestRevision` | `dispute`. After `reviewWindow` it auto-releases. `cancel` refunds unfunded or unstarted milestones. The arbiter resolves disputes with a split.
- The platform fee goes in bps to a treasury, and events drive the indexer. On release, post an ERC-8004 feedback entry.
- **`sowHash` binds the signed SOW** (EIP-712 typed "Contract" signed by client and provider/platform) to the escrow. Store the SOW doc in Postgres or S3 as well.
- A2A: a hired agent can open a child engagement (subcontract) funded from its own wallet, or pay per call via x402. Spend caps are enforced by the runtime wallet policy.

**5b. Scoping agent (`services/scoping`).** An Agent SDK loop with tools: read the intake, inspect linked repos and docs (read-only), web search, the catalog/pricing lookup, and a question-asker. It outputs a **structured Scope** (JSON schema): restated objectives, deliverables, milestones with acceptance criteria, assumptions, **missing inputs/blockers**, risks, feasibility score, duration range, cost estimate (compute + model tokens + vendor fees + margin), and the recommended agent(s). The client edits and approves it, the Scope becomes the SOW, and the SOW gets signed.

**5c. Runtime / VM orchestration (`services/orchestrator`).** Recommendation: **Fly Machines.**
- Why it fits "standby until hired": a Machine is created from the agent's image, then *stopped*, which costs only rootfs/volume storage. It starts in seconds on hire, runs for days, gets a persistent **volume for specialist memory**, per-Machine secrets, private networking and a straightforward REST API.
- E2B and Modal Sandboxes are good for short tasks, but session caps (hours) and less natural long-lived state hurt multi-week engagements. Vercel Sandbox is similar: ephemeral with a duration limit. Plain VMs (Hetzner/EC2) are cheapest per hour but cost the most to operate.
- Pragmatic path: **Fly for engagements, E2B optional for sub-hour "quick tasks"**. Put the provider behind a `Runtime` interface (`provision/start/stop/snapshot/exec/logs`) so it can be swapped. Verify current limits and pricing before committing.
- The in-VM **agent harness** pulls the brief (SOW, milestones, repo creds), runs the configured agent (Claude Agent SDK / Claude Code headless, Codex CLI, OpenHands…), streams logs and heartbeats, calls `submit(milestone, evidence)`, and holds a policy-limited wallet key: a per-engagement budget, a session key or smart-account allowance, never the platform key.

**5d. Agent manifest (`agent.yaml`).** agent_pack.py is *not* a starting point, since it generates fake rosters. Start fresh and split public from private:
- **Public** goes in the ERC-8004 registration file (`agentURI`): name, description, services/endpoints (MCP, A2A, web), supported trust models, pricing model.
- **Private** is platform-only: `persona`/system prompt, `models` (primary/fallback), `skills` (paths or refs), `plugins`, `mcp_servers` (with secret refs), `tools` (base image or devcontainer, libraries), `web_search`, `memory` (volume size, seed knowledge packs), `runtime` (cpu/mem/gpu, max duration, idle policy), `wallet_policy` (caps, allowed counterparties), `sow_templates` accepted, `evals` (acceptance smoke tests run in verification), `vendor` (for Verified Agents).
- Validate it with a JSON Schema and version it. The seller wizard edits the manifest.

**5e. MCP server (`packages/mcp`).** Tools: `search_agents`, `get_agent`, `request_scope(intake)`, `get_scope`, `approve_scope`/`sign_contract`, `fund_engagement` (returns an EIP-712 payload for the user's wallet, or uses a delegated budget key), `get_engagement_status`, `list_milestones`, `accept_milestone`/`request_revision`, `message_agent`, `get_deliverables`. It ships as stdio for Claude Code plus remote Streamable HTTP with OAuth. **Money-moving tools require explicit user confirmation**, and the default is "prepare transaction, user signs".

**5f. Verified Agents catalog.** A `Vendor` model plus a per-vendor adapter (`start_job`, `poll`, `fetch_artifacts`, `cost`). Split vendors into two tiers:
- **Tier A** (API/CLI accessible, BYO key or partner account): Claude Code / Agent SDK, OpenAI Codex, OpenHands (OSS), Devin API, Cursor background agents, Factory Droids, possibly Replit.
- **Tier B** (enterprise, sales-led): Harvey, Legora, Basis, Rogo, 11x, Artisan, Sierra, Decagon, Hippocratic AI, XBOW, Pactum, Keelvar, Paraform, Ema. These get **listing and referral only** until there is a partnership; don't run them on your own subscription.

"Verified" should mean *vendor-attested*, backed by a signed vendor attestation linked from their ERC-8004 identity, not "we logged into their product".

## 6. Stack decision

**Recommendation: keep Python + Flask (restructured) for the backend and server-rendered UI through Phase 4. Add HTMX for interactivity, Foundry for contracts, and a Python MCP server (FastMCP). Do not migrate to Next.js now.**

| | Keep Flask/Jinja (restructured) | Migrate to Next.js + Python workers |
|---|---|---|
| Launch speed | Fastest. Reuses models, auth, config guards, CSS, about 15 templates, chain code | +3–5 weeks before new features; about 11.6k template lines redone |
| Where the hard work is | Scoping, escrow, orchestrator, MCP: backend work, same in either stack | Same backend work; the rewrite buys none of it |
| Wallet UX | ethers UMD + vanilla JS is clunky but works | wagmi/viem/RainbowKit is much better for signing flows |
| Ecosystem | Python has the Agent SDK, MCP SDK, x402 SDK, web3.py | TS is first-class for x402, MCP and viem |
| Risk | `app.py` monolith and in-memory state need a disciplined split in Phase 0 | Two languages, two deploys |

Honest take: most agenthire templates get rebuilt because the flows change, so the UI reuse is real but modest. The deciding factor is that **the missing value is backend and orchestration, and Python serves it fine.** Keep a clean JSON API from day one so a Next.js front end can be dropped in later, around Phase 5 or public launch, if wallet UX demands it. If a team member is strongly TS-native, a full TS monorepo is also defensible. Just don't do it half-way.

## 7. Repo structure and roadmap

```
agents-list/
├── app/                    # Flask app factory + blueprints
│   ├── catalog/  engagements/  seller/  admin/  api/  chain/
│   ├── models/             # SQLAlchemy + alembic migrations/
│   ├── templates/  static/
│   └── config.py extensions.py auth.py
├── chain/                  # web3 client (from onchain.py), x402 facilitator, erc8004 client, indexer
├── contracts/              # Foundry: EngagementEscrow.sol, MockUSDC3009.sol, script/, test/
├── services/
│   ├── scoping/            # Agent SDK scoping agent + Scope schema
│   └── orchestrator/       # Runtime interface, Fly adapter, job queue worker
├── harness/                # in-VM agent runner image (Dockerfile, entrypoint)
├── manifests/              # agent.schema.json + example specialist manifests
├── vendors/                # Verified-Agent adapters
├── mcp/                    # FastMCP server (stdio + HTTP)
├── tests/
├── NOTICE / LICENSE        # see §8
└── docker-compose.yml      # web + worker + postgres + redis
```

**Phases**
- **Phase 0, fork and cleanup (≈1 wk):** sort out the license first (§8). Import the history (or fresh-start with attribution), delete the DROP list, split `app.py` into blueprints, go Postgres + Alembic, and get the tests green.
- **Phase 1, Sepolia (≈1 wk):** chain config, Circle USDC, ERC-8004 registration of listed agents, self-hosted x402 facilitator on `eip155:11155111`, Etherscan links, async tx status.
- **Phase 2, scoping + SOW + escrow (≈2–3 wks):** intake form, scoping agent, Scope→SOW editor, EIP-712 contract signing, `EngagementEscrow` with milestones, and an engagement page that replaces `order.html`.
- **Phase 3, VM runtime (≈2–3 wks):** manifest schema, Fly orchestrator, harness image, standby pool, log streaming, milestone submission from the VM, a wallet policy.
- **Phase 4, MCP (≈1 wk):** FastMCP server over the JSON API, confirmation-gated payment tools, a Claude Code install doc.
- **Phase 5, Verified Agents (ongoing):** Vendor model, Tier A adapters (Claude Code, Codex, OpenHands first), vendor attestation flow, Tier B referral listings.

**First ~10 commits of `agents-list`**
1. Import agenthire source with attribution (NOTICE and upstream commit ref), plus the agreed LICENSE.
2. Remove the sim engine, demo/sim/agent-mode pages, ICM, auctions, seed packs and demo docs.
3. Replace the in-memory `AGENTS` list with DB queries and drop the junk-name filters.
4. Split `app.py` into catalog/seller/admin/api/chain blueprints behind an app factory.
5. Add Alembic migrations and a Postgres docker-compose, and remove the `_ensure_columns` ALTER hack.
6. Centralize chain config: Sepolia defaults, an `explorer_url()` helper, and a `snowtrace`→`explorer` key rename.
7. Switch payments to Circle Sepolia USDC and read the EIP-712 domain from the contract.
8. Add a Foundry project with `EngagementEscrow` (milestones, receiveWithAuthorization) and tests.
9. Replace the custom registry/reputation with ERC-8004 Identity/Reputation clients on Sepolia.
10. Add the Engagement/Milestone/Scope models and the intake→scope→sign→fund flow skeleton.

## 8. Open questions and risks

1. **Licensing: blocking.** agenthire has **no LICENSE file**, so the default is all rights reserved. Public on GitHub ≠ permission to reuse. Contributors are Nicholas Hardy (109 commits), Shalin Patel (13, repo owner), Tharun Ekambaram (8, some from a **coinbase.com** email, so check employer-IP exposure) and `nichar3232`. Get written permission, or have the owner add MIT/Apache-2.0, before copying code. Otherwise treat this as a clean-room reimplementation that borrows only ideas. Keep a NOTICE crediting the team either way.
2. **Verified-Agents ToS:** reselling or sharing a vendor subscription or seat (running Devin/Harvey/etc. under your account for third parties) likely breaches most vendor terms. Using vendor names or logos as "Verified" implies endorsement (trademark risk). Use BYO-key, official partner/reseller programs, or referral-only listings.
3. **Money transmission and custody:** holding client funds in escrow and taking a fee can raise money-transmitter/MSB questions at mainnet time. Testnet is fine; get counsel before real USDC.
4. **Liability for agent output:** SOW terms, IP assignment of deliverables, confidentiality of client repos on VMs, and dispute arbitration all need a standard MSA/SOW template.
5. **Security:** VMs hold client credentials and wallet keys. Use per-engagement secrets, egress controls, a spend cap per wallet, a kill switch, and audit logs. Prompt injection from client repos and web content can drive a funded agent to spend or exfiltrate. Payment tools stay confirmation-gated.
6. **Escrow contract risk:** new unaudited Solidity. Keep it minimal and pausable, test it well, and get an audit before mainnet.
7. **Sepolia practicalities:** Circle faucet limits, ETH faucet friction, 12s blocks and gas spikes, and no hosted x402 facilitator for Sepolia (we self-host). Final mainnet target TBD: Ethereum L1 gas makes milestone txs pricey, so consider Base or Arbitrum for production, where the same code and the ERC-8004 registries exist.
8. **Scoping accuracy:** cost and duration estimates for agent work are unproven. Use estimate ranges plus per-milestone pricing plus a change-order flow, and track estimate-vs-actual from day one.
9. **Decide:** a platform-operated agent pool vs. third-party sellers running their own images (trust, image scanning, revenue split), and whether the MCP server acts with a delegated budget key or always defers signing to the human.
