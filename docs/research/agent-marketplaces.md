# Existing agent marketplaces and related projects

Survey date: 2026-09-26. URLs were checked on that date. "(unverified)" marks
claims taken from secondary sources or that couldn't be confirmed. Traction
figures are self-reported by each project unless noted.

## Summary

- **Nobody combines the whole flow.** No product (crypto or not) does all of: a
  scoped SOW before the job → milestone escrow → an independent specialist agent
  on its own VM doing multi-day work → delivery-based reputation → hiring from a
  local agent over MCP. The pieces exist, spread across different players.
- **Closest competitors:** NEAR AI Agent Market (task → bid → escrow → dispute
  agent), OKX.AI (identity + escrow contracts + evaluators + reputation), and
  Virtuals ACP (on-chain jobs with escrow and an evaluator, but organic usage now
  looks very low). All three start from an already-defined job; none scopes one.
- **Standards to align with:** ERC-8004 (identity/reputation, live on mainnet
  since 2026-01-29) and **ERC-8183 "Agentic Commerce"** (a draft job-escrow
  standard: client/provider/evaluator, Open → Funded → Submitted → Terminal).
  ERC-8183 overlaps directly with `EngagementEscrow`: we should decide whether to
  be ERC-8183-compatible per milestone. x402 now lives at
  `x402-foundation/x402` (Linux Foundation).
- **Enterprise stores** (Salesforce, AWS, Google, Microsoft, Oracle, ServiceNow,
  Workday, Claude Marketplace) are catalogs inside one platform, sold as seats,
  subscriptions or usage. No SOW, escrow or cross-platform reputation.
- **Long-running agents on their own VMs** (Devin, Jules, Codex cloud, Claude Code
  on the web, Manus, OpenHands Cloud) are single-vendor, first-party agents, not
  marketplaces.
- **Upwork's MCP server (2026-08-10)** lets Claude and other agents post jobs and
  hire, with escrow and milestones. This proves the demand for MCP hiring, but
  the workers are humans.
- **Name collision:** aiagentslist.com ("AI Agents List") is an existing agent
  directory with a name close to ours.

## Closest competitors

| Product | Model | Escrow | Scoping / SOW | Reputation | Runtime | Local-agent access | Status |
|---|---|---|---|---|---|---|---|
| [NEAR AI Agent Market](https://market.near.ai/) | Post task → agents bid → escrow → deliver → dispute agent | Yes, single job | Bids only | Unclear | Agent's own | Claims Claude/Codex/OpenClaw support (MCP unverified) | Live since 2026-02 |
| [OKX.AI](https://okx.ai/) | Agent marketplace + task marketplace | Yes, escrow contracts (USDT/USDG) | No | Yes, carries across job types | Unclear | Unverified | Public since 2026-06-30 |
| [Virtuals ACP](https://app.virtuals.io/acp) | On-chain job phases with an evaluator | Yes, single job | Negotiation phase | Ratings | Agent's own | SDKs, chat front end (Butler) | Live on Base; claims 1.77M jobs, but ~17 events/day on 2026-09-23 per agenteconomy.to |
| [AI Agent Store "Claw Earn"](https://aiagentstore.ai/) | Funded task rewards | Yes, USDC on Base | No | No | Hosted agents | No | Live |
| [Upwork MCP](https://www.upwork.com) | Hire humans via MCP | Yes, milestones | Job posts | Job success score | n/a | Yes, MCP | Live since 2026-08 |
| [Olas Mech + Pearl](https://olas.network/) | Pay-per-request between agents; desktop agent store | No | No | No | User's machine | No | Live; 18.2M tx |

## 1. Crypto / on-chain marketplaces and protocols

### Hiring and marketplaces
- **Virtuals Protocol — Agent Commerce Protocol (ACP)**:
  [virtuals.io](https://www.virtuals.io/) · [docs](https://whitepaper.virtuals.io/about-virtuals/agent-commerce-protocol-acp) ·
  [acp-node](https://github.com/Virtual-Protocol/acp-node) · [acp-python](https://github.com/Virtual-Protocol/acp-python).
  Jobs between agents run on-chain: request → negotiation → escrow → evaluation → settlement.
  v2 (Feb 2026) added hooks, persistent accounts between two agents, and multiple chains.
  It's heavily token and incentive driven.
- **ERC-8183 "Agentic Commerce"**: [EIP](https://eips.ethereum.org/EIPS/eip-8183) ·
  [base-contracts](https://github.com/erc-8183/base-contracts). A minimal job
  escrow proposed by Virtuals. It deliberately leaves out negotiation, milestones
  and disputes. Circle's Arc supports it.
- **NEAR AI Agent Market**: [market.near.ai](https://market.near.ai/) ·
  [launch post](https://near.ai/blog/introducing-near-ai-agent-market). "Hire
  agents like teammates": bidding, escrow, delivery hash, dispute agent. Sources
  disagree on payment currency (NEAR vs USD/USDC).
- **OKX.AI**: [okx.ai](https://okx.ai/). Exchange-backed. One persistent
  on-chain identity per agent, services and tasks marketplaces, instant or
  escrow-backed payment, and a network of evaluators for disputes. Chain and
  ERC-8004 use unverified.
- **Olas / Autonolas**: [Mech Marketplace](https://olas.network/agent-economies/mech) ·
  [Pearl](https://olas.network/pearl) · [valory-xyz/mech](https://github.com/valory-xyz/mech).
  Agent-to-agent pay-per-request, plus a desktop agent app store. 15% protocol fee.
- **Fetch.ai Agentverse / ASI:One**: [agentverse.ai](https://agentverse.ai/) ·
  [asi1.ai](https://asi1.ai/) · [uAgents](https://github.com/fetchai/uAgents).
  Registry, hosting and a chat front end. Payments via Visa, USDC or FET since early 2026.
- **SingularityNET AI Marketplace**: [marketplace.singularitynet.io](https://marketplace.singularitynet.io/).
  The original pay-per-call AI services market. Now legacy.
- **Warden Protocol Agent Hub**: [wardenprotocol.org](https://wardenprotocol.org/).
  A wallet-based "app store for agents", mostly trading/DeFi.
- **OpenServ**: [x402 services](https://platform.openserv.ai/x402-services).
  Agents and workflows sold as pay-per-use x402 services.
- **Masumi Network** (Cardano): [masumi.network](https://www.masumi.network/).
  Registry, decentralized IDs, and an escrow payment standard (MIP-003).
- **Theoriq** ([theoriq.ai](https://www.theoriq.ai/)), **Talus** ([talus.network](https://talus.network/)),
  **Morpheus** ([mor.org](https://mor.org/)), **Recall** ([recall.network](https://recall.network/)),
  **Bittensor agent subnets** ([subnet index](https://subnetradar.com/research/subnets)):
  adjacent. They cover DeFi agent groups, on-chain workflows, compute markets,
  leaderboard-based ranking, and incentives to produce agents. None does hiring with escrow.

### Identity and reputation (ERC-8004)
- **ERC-8004 Trustless Agents**: [EIP](https://eips.ethereum.org/EIPS/eip-8004) ·
  [8004.org](https://www.8004.org/) · [contracts](https://github.com/erc-8004/erc-8004-contracts).
  Identity, reputation and validation registries. Mainnet since 2026-01-29; also on Sepolia.
- **8004scan**: [8004scan.io](https://8004scan.io/). Explorer across ERC-8004
  registries: ~539k agents and ~626k feedback entries, mostly unverified or spam.
- **Phala ERC-8004 TEE agent**: [repo](https://github.com/Phala-Network/erc-8004-tee-agent).
  Agents in Intel TDX confidential VMs with attestation. The closest match to
  "agents on their own VMs".
- **ChaosChain**: [chaoscha.in](https://chaoscha.in/). Verification of agent work
  on top of ERC-8004. Prototype stage.
- **Assay Protocol**: [repo](https://github.com/0xvikram/assay). Staked
  registration and escrow, with ERC-8004 feedback posted after each settlement.
  Early and small, but it's the escrow → reputation loop we want.

### Payments and discovery
- **x402 + Bazaar**: [x402.org](https://www.x402.org/) ·
  [Bazaar](https://docs.cdp.coinbase.com/x402/bazaar) ·
  [x402-foundation/x402](https://github.com/x402-foundation/x402). Payment over
  HTTP 402, plus a discovery index and MCP server. Pay per call, no escrow.
  ~165M transactions by Apr 2026.
- **Google AP2**: [ap2-protocol.org](https://ap2-protocol.org/) ·
  [AP2](https://github.com/google-agentic-commerce/AP2) ·
  [a2a-x402](https://github.com/google-agentic-commerce/a2a-x402). Signed
  payment authorizations ("mandates"); v0.2 in Apr 2026.
- **Stripe/Tempo Machine Payments Protocol**: [mpp.dev](https://mpp.dev/).
  HTTP 402 with streaming sessions, which could suit long-running jobs.
- **Nevermined** ([nevermined.ai](https://nevermined.ai/)), **Skyfire / KYAPay**
  ([kyapay.org](https://kyapay.org/)), **Payman** ([paymanai.com](https://paymanai.com/)),
  **Crossmint** ([crossmint.com](https://www.crossmint.com/)): agent billing,
  identity-plus-payment tokens, agent wallets with spending policies, card and
  stablecoin checkout.
- **Circle Arc escrow + Refund Protocol**: [docs](https://docs.arc.io/build/agentic-economy) ·
  [arc-escrow](https://github.com/circlefin/arc-escrow) ·
  [refund-protocol](https://github.com/circlefin/refund-protocol). USDC
  escrow with arbiter refunds and AI-checked delivery. The closest reference
  design for `EngagementEscrow`.

### Small escrow / hiring projects
- [Agoragentic](https://agoragentic.com/): agent-to-agent API marketplace with an internal USDC ledger; 3% fee.
- [AIProx](https://aiprox.dev/): open MCP registry for hiring agents (claims 16 agents).
- [dealwork.ai](https://dealwork.ai/), [toku.agency](https://toku.agency/), [ClawGig](https://clawgig.ai/): small job boards with escrow (Stripe or Base USDC).
- [Kustodia](https://kustodia.mx/): an escrow contract exposed as MCP tools (unverified).
- [agent-hire-mcp](https://github.com/Rumblingb/agent-hire-mcp), [paycrow](https://github.com/michu5696/paycrow): tiny GitHub projects with the same concept.

## 2. Enterprise and mainstream marketplaces

### Enterprise catalogs
- [Salesforce AgentExchange](https://agentexchange.salesforce.com/): 1k+ agents, tools and MCP servers; billing and security review.
- [AWS Marketplace — AI Agents & Tools](https://aws.amazon.com/marketplace/solutions/ai-agents-and-tools): deploy to AWS's agent runtime (Bedrock AgentCore); MCP and A2A tagging.
- [Google Cloud Marketplace](https://cloud.google.com/marketplace) + [Gemini Enterprise Agent Gallery](https://docs.cloud.google.com/gemini/enterprise/docs/agent-gallery): partner agents validated for A2A.
- [Microsoft Marketplace / M365 Agent Store](https://marketplace.microsoft.com/): buy there, deploy into Copilot.
- [ServiceNow AI Agent Marketplace](https://store.servicenow.com/store/ai-marketplace), [Oracle Fusion AI Agent Marketplace](https://www.oracle.com/applications/fusion-ai/ai-agent-marketplace/), [IBM watsonx Orchestrate catalog](https://www.ibm.com/products/watsonx-orchestrate/agent-catalog), [Workday agents](https://marketplace.workday.com/en-US/pages/agents), [SAP Joule agents](https://www.sap.com/products/artificial-intelligence/ai-agents.html), [HubSpot Breeze agents](https://ecosystem.hubspot.com/marketplace/breeze-agents/all).
- [Claude Marketplace](https://claude.com/marketplace): relaunched 2026-09-23 with 2,000+ connectors, agents and partners; bought against the enterprise's Anthropic commitment.

### Consumer and prosumer
- [ChatGPT apps / Plugin directory](https://chatgpt.com/gpts): Apps SDK built on MCP; shared between ChatGPT and Codex since 2026-07.
- Agent.ai: "LinkedIn for agents". On 2026-09-26 agent.ai redirected to builderpack.com; current status unclear.
- [Poe](https://poe.com): creators set a price per message on bots.

### Directories
- [AI Agents Directory](https://aiagentsdirectory.com/) (~2.9k), [AI Agent Store](https://aiagentstore.ai/) (1.3k+, plus hosted agents and Claw Earn escrow task rewards), [AI Agents List](https://aiagentslist.com/) (~620, "Founder Verified" badge), [agent.market](https://agent.market/) (content unclear).

### Freelance platforms
- [Upwork](https://www.upwork.com): MCP server (2026-08-10) and the Uma work agent. Escrow, milestones and reputation, but for humans.
- [Fiverr Go](https://www.fiverr.com/news/fiverr-go): AI models trained on a seller's own work; agentic fulfillment is still a pilot.
- [UpAgents](https://upagents.app/): "Upwork for AI agents", flat price per task, no escrow.

### Long-running agent services (single vendor)
- [Devin](https://devin.ai/), [Factory](https://factory.ai/), [Manus](https://manus.im/), [Genspark](https://www.genspark.ai/),
  [OpenAI Codex](https://openai.com/codex/), [Claude Code on the web / Routines / Managed Agents](https://code.claude.com/docs/en/claude-code-on-the-web),
  [Jules](https://jules.google/), [OpenHands Cloud](https://www.openhands.dev/), [Relevance AI](https://relevanceai.com/),
  [Lindy](https://www.lindy.ai/), [CrewAI AMP marketplace](https://docs.crewai.com/en/enterprise/features/marketplace).
- Outcome-priced support agents set a precedent for paying per outcome: [Sierra](https://sierra.ai/), Intercom Fin ($0.99 per resolution), Decagon.

### MCP and A2A registries
- [Official MCP Registry](https://registry.modelcontextprotocol.io/) ([repo](https://github.com/modelcontextprotocol/registry)), [Smithery](https://smithery.ai/), [Glama](https://glama.ai/mcp/servers) (health checks and quality scores), [mcp.so](https://mcp.so/), [PulseMCP](https://www.pulsemcp.com/).
- [A2A](https://a2a-protocol.org/) ([repo](https://github.com/a2aproject/A2A)): spec 1.0, Agent Cards at `/.well-known/agent-card.json`; no dominant public directory.
- [ClawHub](https://clawhub.ai/): a skills marketplace where 341 of 2,857 skills were found to be malicious (Jan 2026). A warning for our verification design.

### Non-crypto payment rails
- [Stripe ACP](https://docs.stripe.com/agentic-commerce/acp) ([spec](https://agenticcommerce.dev/)), [Google AP2](https://ap2-protocol.org/) / [UCP](https://developers.google.com/merchant/ucp), [Visa Intelligent Commerce](https://www.visa.com/en-us/solutions/intelligent-commerce), [Mastercard Agent Pay](https://www.mastercard.com/us/en/business/artificial-intelligence/mastercard-agent-pay.html). All are built for one-off checkout, not holding funds against milestones.

## 3. Open-source projects (GitHub)

Stars and last-push dates as of 2026-09-26.

### Competing or adjacent marketplaces
- [internet-court/internet-court-skill](https://github.com/internet-court/internet-court-skill) (~6.1k★): a Claude Code plugin/skill for agent commerce (mandates, ERC-7710, x402, escrow, disputes). Very new repo with a high star count; treat with caution.
- [Virtual-Protocol ACP SDKs](https://github.com/Virtual-Protocol), [valory-xyz/mech](https://github.com/valory-xyz/mech), [fetchai/uAgents](https://github.com/fetchai/uAgents) (1.6k★).
- [microsoft/multi-agent-marketplace](https://github.com/microsoft/multi-agent-marketplace) (188★): simulator of agent-driven markets; a research reference.
- [aiagenta2z/ai-agent-marketplace](https://github.com/aiagenta2z/ai-agent-marketplace): an open agent directory with no payments.

### Standards we build on
- [erc-8004/erc-8004-contracts](https://github.com/erc-8004/erc-8004-contracts) (237★), [ChaosChain reference implementation](https://github.com/ChaosChain/trustless-agents-erc-ri), [ethereum/ERCs](https://github.com/ethereum/ERCs).
- [erc-8183/base-contracts](https://github.com/erc-8183/base-contracts) and [hook-contracts](https://github.com/erc-8183/hook-contracts).
- [x402-foundation/x402](https://github.com/x402-foundation/x402) (6.6k★), [a2a-x402](https://github.com/google-agentic-commerce/a2a-x402), [A2A](https://github.com/a2aproject/A2A) (25.9k★), [AP2](https://github.com/google-agentic-commerce/AP2), [MCP registry](https://github.com/modelcontextprotocol/registry).

### Reusable infrastructure
- MCP: [PrefectHQ/fastmcp](https://github.com/PrefectHQ/fastmcp) (27.9k★, formerly jlowin/fastmcp), [python-sdk](https://github.com/modelcontextprotocol/python-sdk), [inspector](https://github.com/modelcontextprotocol/inspector).
- ERC-8004 SDKs: [agent0lab/agent0-py](https://github.com/agent0lab/agent0-py) (Python, could be used directly by `chain/`), [qntx/erc8004](https://github.com/qntx/erc8004) (Rust).
- x402 facilitators: [qntx/facilitator](https://github.com/qntx/facilitator), [local-x402-facilitator](https://github.com/fortylabs/local-x402-facilitator); relevant to issue #4.
- Escrow reference: [circlefin/arc-escrow](https://github.com/circlefin/arc-escrow), [circlefin/refund-protocol](https://github.com/circlefin/refund-protocol); relevant to issue #1.
- Seller-side agent kits: [daydreamsai/lucid-agents](https://github.com/daydreamsai/lucid-agents), [AgentlyHQ/aixyz](https://github.com/AgentlyHQ/aixyz), [coinbase/agentkit](https://github.com/coinbase/agentkit), [nevermined-io/payments-py](https://github.com/nevermined-io/payments-py).
- Runtimes and sandboxes (issue #10): [OpenHands](https://github.com/OpenHands/OpenHands), [E2B](https://github.com/e2b-dev/E2B), [Daytona](https://github.com/daytonaio/daytona), [microsandbox](https://github.com/superradcompany/microsandbox), [cloudflare/sandbox-sdk](https://github.com/cloudflare/sandbox-sdk), [Modal](https://github.com/modal-labs/modal-client), [flyctl](https://github.com/superfly/flyctl).

### Awesome lists (places to list, sources of sellers)
- [e2b-dev/awesome-ai-agents](https://github.com/e2b-dev/awesome-ai-agents), [500-AI-Agents-Projects](https://github.com/ashishpatel26/500-AI-Agents-Projects), [awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers), [awesome-erc8004](https://github.com/sudeepb02/awesome-erc8004), [awesome-x402](https://github.com/xpaysh/awesome-x402), [awesome-agentic-commerce](https://github.com/Merit-Systems/awesome-agentic-commerce), [Awesome-AI-Agent-Marketplace](https://github.com/ishandutta2007/Awesome-AI-Agent-Marketplace).

## Gaps nobody fills well

1. **Scoping before pricing.** Everyone prices by seat, call, task, reward or
   bid on an already-defined job. Nobody turns a vague goal into a scoped SOW
   with milestones, acceptance criteria, a duration and a cost estimate.
2. **Multi-milestone escrow for long work.** Existing escrow (ACP, ERC-8183,
   NEAR, Claw Earn, Arc) covers one deliverable. Nobody offers per-milestone
   funding, partial release, revisions and change orders for jobs that run
   for days or weeks. Upwork has this for humans only.
3. **Independent specialist agents on dedicated VMs.** Long-running VM agents are
   first-party (Devin, Jules, Codex, Claude Code). Marketplaces either deploy
   into the buyer's own platform or are just catalogs. Phala has attested VMs but
   no hiring flow.
4. **Hiring from a local agent over MCP.** Upwork's MCP server hires humans; x402
   Bazaar pays per call; AIProx and Kustodia are tiny. Nobody lets Claude Code
   scope, fund and track a long engagement with a remote agent.
5. **Reputation from paid, escrow-settled work.** ERC-8004 feedback on 8004scan
   is mostly unverified or spam; enterprise "verification" is vendor
   certification. Only Assay (early) links escrow settlement to ERC-8004
   feedback. Delivery-based reputation that works across platforms is open.

## Implications for the roadmap

- Issue #1 (`EngagementEscrow`): review ERC-8183 and `circlefin/arc-escrow`.
  Consider making each milestone an ERC-8183-shaped job, or record why not in
  `docs/decisions/`.
- Issue #2 (ERC-8004): evaluate `agent0-py` before writing our own client, and
  post feedback only from escrow settlement, so our reputation data can be
  trusted in a way 8004scan's mostly can't.
- Issue #4 (x402 facilitator): point to `x402-foundation/x402`, not `coinbase/x402`.
- Issue #12 (MCP): publish to the official MCP registry and the awesome-mcp-servers list; the
  differentiator is scope → fund → track, not search.
- Issue #13 (Verified Agents): ClawHub's malicious-skill incident argues for
  attestation plus scanning, not self-declared badges.
- Watch: NEAR AI Agent Market and OKX.AI (closest competitors), and Upwork, which
  could add agent workers to an MCP flow that already has escrow and milestones.
