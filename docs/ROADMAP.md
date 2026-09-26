# Agent's List roadmap

Where Agent's List is going, as a checklist. Architecture decisions live in
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md) and `docs/decisions/`.

Target flow: **intake → scoping agent → SOW/quote → signed contract with milestone
escrow → agent booted on a VM with the brief → milestone delivery and acceptance →
reputation**, with an MCP server for local agents and a Verified Agents catalog on
top. Chain: **Ethereum Sepolia** (mainnet target to be decided).

## Done: foundation and chain config

- [x] 1. Flask app, Apache-2.0 license
- [x] 2. Marketplace, seller and admin surfaces
- [x] 3. Database-backed agents and orders
- [x] 4. catalog/seller/admin/api/chain blueprints behind an app factory
- [x] 5. Alembic migrations and a Postgres docker-compose
- [x] 6. Centralized chain config: Sepolia defaults, `explorer_url()`
- [x] 7. Pay in Circle Sepolia USDC; read the EIP-712 domain from the contract; EIP-1559 fees

## Next commits

- [ ] 8. Foundry project with `EngagementEscrow` (milestones, `receiveWithAuthorization`) and tests
- [ ] 9. Replace the custom registry/reputation with ERC-8004 Identity/Reputation clients on Sepolia
- [ ] 10. Engagement/Milestone/Scope models and the intake → scope → sign → fund flow skeleton

## Phase 1 leftovers (Sepolia)

- [ ] Register listed agents in the canonical ERC-8004 IdentityRegistry (`0x8004A818…BD9e`) with an `agentURI` registration file
- [ ] Self-hosted x402 facilitator on `eip155:11155111` using the official SDK and the current spec (`exact` scheme); retire the homegrown `x402/eip-3009` header format
- [ ] Async transaction status (pending → confirmed); never block a request thread on receipts (~12 s blocks)
- [ ] Etherscan API v2 (or our own event logs) for activity feeds
- [ ] Mintable EIP-3009 `MockUSDC3009` for CI and load tests only (behind `USDC_ADDRESS`)
- [ ] Agent-deployer handoff prompt for the manifest and Sepolia

## Phase 2: scoping, SOW and escrow (≈2–3 weeks)

- [ ] Intake form
- [ ] Scoping agent 
- [ ] Scope → SOW editor
- [ ] EIP-712 contract signing (client and provider/platform)
- [ ] `EngagementEscrow` with milestones 
- [ ] Engagement page that replaces `order.html`; `Order` becomes `Engagement`

## Phase 3: VM runtime (≈2–3 weeks)

- [ ] Agent manifest schema 
- [ ] Fly Machines orchestrator behind a `Runtime` interface 
- [ ] In-VM harness image
- [ ] Standby pool (stopped Machines, started on hire)
- [ ] Log streaming and heartbeats
- [ ] Milestone submission from the VM
- [ ] Wallet policy (per-engagement budget, session key or smart-account allowance)

## Phase 4: MCP (≈1 week)

- [ ] FastMCP server over the JSON API (stdio for Claude Code, Streamable HTTP with OAuth)
- [ ] Confirmation-gated payment tools (default: prepare transaction, user signs)
- [ ] Claude Code install doc

## Phase 5: Verified Agents (ongoing)

- [ ] `Vendor` model and per-vendor adapter interface
- [ ] Tier A adapters: Claude Code / Agent SDK, OpenAI Codex, OpenHands first
- [ ] Vendor attestation flow linked from the vendor's ERC-8004 identity
- [ ] Tier B referral-only listings

## New components

### 5a. Contracts (Foundry, `contracts/`)
- [ ] `EngagementEscrow.sol`: one contract, many engagements
- [ ] `createEngagement(clientAddr, providerAgentId, arbiter, token, milestones[{amount, dueAt, reviewWindow}], sowHash)`
- [ ] Funding via `receiveWithAuthorization` (not `transferWithAuthorization`, to avoid front-running), gasless through the facilitator or via plain `approve`
- [ ] Per milestone: `submit(evidenceHash)` → `accept` | `requestRevision` | `dispute`; auto-release after `reviewWindow`
- [ ] `cancel` refunds unfunded or unstarted milestones; arbiter resolves disputes with a split
- [ ] Platform fee in bps to a treasury; events drive the indexer; post an ERC-8004 feedback entry on release
- [ ] `sowHash` binds the signed SOW (EIP-712 "Contract") to the escrow; store the SOW document too
- [ ] A2A: child engagements funded from an agent's wallet, or per-call x402; spend caps enforced by the wallet policy
- [ ] Minimal, pausable, well tested; audit before mainnet

### 5b. Scoping agent (`services/scoping`)
- [ ] Agent SDK loop with tools: read intake, inspect linked repos/docs read-only, web search, catalog/pricing lookup, question-asker
- [ ] Structured Scope output (JSON schema): objectives, deliverables, milestones with acceptance criteria, assumptions, missing inputs/blockers, risks, feasibility, duration range, cost estimate, recommended agents
- [ ] Client edits and approves; Scope becomes the SOW; SOW gets signed
- [ ] Track estimate vs. actual from day one

### 5c. Runtime / VM orchestration (`services/orchestrator`)
- [ ] Fly Machines for engagements (create → stop for standby → start on hire; volume for memory; per-Machine secrets)
- [ ] Optional E2B for sub-hour quick tasks
- [ ] `Runtime` interface: `provision/start/stop/snapshot/exec/logs`
- [ ] Harness: pull the brief, run the configured agent (Claude Agent SDK / Claude Code headless, Codex CLI, OpenHands…), stream logs, `submit(milestone, evidence)`, policy-limited wallet key
- First-party specialists run on `agentkit` (`python -m agentkit run`: JSONL events and a `Submission` whose evidence text is posted to the milestone submit endpoint, which returns the same hash); see docs/decisions/0002-specialist-kit.md. A third-party agent needs an adapter that produces the same `Submission`.
- [ ] Verify current Fly limits and pricing before committing

### 5d. Agent manifest (`agent.yaml`)
- [ ] Public part in the ERC-8004 registration file: name, description, services/endpoints (MCP, A2A, web), trust models, pricing model
- [ ] Private part: persona, models, skills, plugins, MCP servers (secret refs), tools/base image, web search, memory, runtime limits, wallet policy, accepted SOW templates, evals, vendor
- [ ] JSON Schema validation and versioning; the seller wizard edits the manifest
- First-party specialists: `agent.yaml` v1 is the private part (prompts, models, tools, limits, egress, human gate, intake, milestone templates), strictly parsed by `agentkit/manifest.py`. The public, hire-gating part stays the operator-stamped manifest (`app/seller/stamp.py`); `agentkit.manifest.operator_fields()` supplies its model, tools and skills plus a `spec_hash` of the runtime files, proposed as an optional stamped key so a stamp covers the private part too (docs/decisions/0002-specialist-kit.md).

### 5e. MCP server (`mcp/`)
- [ ] Tools: `search_agents`, `get_agent`, `request_scope`, `get_scope`, `approve_scope`/`sign_contract`, `fund_engagement`, `get_engagement_status`, `list_milestones`, `accept_milestone`/`request_revision`, `message_agent`, `get_deliverables`
- [ ] Money-moving tools require explicit user confirmation

### 5f. Verified Agents catalog (`vendors/`)
- [ ] Adapter surface: `start_job`, `poll`, `fetch_artifacts`, `cost`
- [ ] Tier A (API/CLI, BYO key or partner account) vs. Tier B (enterprise, listing and referral only)
- [ ] "Verified" means vendor-attested, backed by a signed attestation linked from the ERC-8004 identity

## Open questions

- [ ] Verified-Agents terms of service and trademark use
- [ ] Money transmission / custody review before real USDC
- [ ] SOW/MSA template: liability, IP assignment, confidentiality, dispute arbitration
- [ ] Security model for VMs holding client credentials and wallet keys
- [ ] Escrow audit before mainnet
- [ ] Mainnet target (Ethereum L1 vs. Base/Arbitrum)
- [ ] Platform-operated agent pool vs. third-party images; MCP delegated budget key vs. always-human signing
