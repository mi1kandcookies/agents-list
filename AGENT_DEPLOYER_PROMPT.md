# AgentHire - Agent Deployer Handoff Prompt

Hand this file (or the `Prompt` section below) to any wallet holder who wants
to list a live agent on AgentHire. It contains everything an AI coding assistant
needs to register, stake, and list an agent on-chain with zero follow-up
questions - assuming the deployer has a funded Avalanche wallet and access to
the AgentHire repo.

The prompt is deliberately self-contained: it pins the network, lists the
contracts by their env-var name (not hex), and restricts the assistant to the
endpoints the backend already exposes.

---

## Prompt

> You are helping me register a new AI agent on the AgentHire protocol and
> list it live. I am the deployer and I will sign all transactions from my
> own wallet.
>
> ### My environment
> - Repo: `agenthire/` (Flask + Python on-chain layer at `onchain.py`).
> - Network: Avalanche C-Chain (`CHAIN_ID=43114`) unless I tell you otherwise.
>   For Fuji dev, set `CHAIN_ID=43113` and `RPC_URL=https://api.avax-test.network/ext/C/rpc`.
> - Contracts are referenced by the env vars documented in `.env.example`:
>   `MOCK_USDC_ADDRESS`, `AGENT_REGISTRY_ADDRESS`, `REPUTATION_ADDRESS`,
>   `STAKING_ADDRESS`, `ESCROW_ADDRESS`, `AUCTION_ADDRESS`. Read them from my
>   local `.env`; DO NOT hard-code any hex address in code you write.
> - My wallet private key is in `DEPLOYER_PRIVATE_KEY`. Never echo, print, or
>   log this value. Never copy it into a file you commit. Use it only to sign
>   transactions in-process via `eth_account`.
>
> ### My agent
> Fill these in BEFORE you run anything. If any are missing, stop and ask me
> exactly one clarifying question per missing field.
>
> 1. `name` - display name of the agent (≤ 64 chars).
> 2. `endpointURL` - HTTPS URL the marketplace will POST jobs to.
> 3. `category` - one of: Development, Data & Analytics, Content, Finance,
>    Research, Security, Automation.
> 4. `use_case` - free-form short tag (e.g. "Code Review").
> 5. `billing` - `per_token` or `per_minute`.
> 6. `min_price`, `max_price`, `current_price` - floats in USDC.
> 7. `stake_usdc` - integer ≥ 100 (Tier 1), 500 (Tier 2), 2000 (Tier 3).
> 8. `verification_tier` - `basic` ($10) or `thorough` ($50).
>
> ### Steps
> Do each of these atomically and STOP on the first failure. After every
> on-chain tx, print the Snowtrace URL and wait for a confirmation receipt
> before moving on.
>
> 1. **Load config.** Import `onchain.get_deployment()` and verify every
>    `*_ADDRESS` env var is set to a non-zero address on the configured
>    `CHAIN_ID`. If any address is missing, bail out with a clear error naming
>    the missing env var.
>
> 2. **Fund + sanity-check wallet.** Using `DEPLOYER_PRIVATE_KEY`, derive the
>    deployer address. Confirm a nonzero AVAX balance for gas. Confirm USDC
>    balance ≥ `stake_usdc + 50` (covers stake + listing + buffer). If low,
>    tell me the exact amount to send and STOP - do not auto-mint on mainnet.
>
> 3. **Register on `AgentRegistry`.** Call `registerAgent(wallet, name,
>    endpointURL)` signed by `DEPLOYER_PRIVATE_KEY`. Parse the emitted
>    `AgentRegistered(agentId, ...)` event and save `agentId` to a local
>    `deployment.json` (gitignored).
>
> 4. **Stake.** `MockUSDC.approve(STAKING_ADDRESS, stake_usdc * 1e6)` then
>    `StakingSlashing.stake(agentId, stake_usdc * 1e6)`. Reject anything below
>    the Tier 1 floor (100 USDC). Print tier reached.
>
> 5. **Publish listing.** POST `/seller/create` to the running Flask backend
>    with the fields above, OR call the on-chain listing update if we've added
>    `AgentRegistry.updateListing` since this prompt was written. Either way,
>    confirm via `GET /api/agents/<agentId>` that the agent now appears in the
>    marketplace.
>
> 6. **Smoke-test a payment.** Sign a tiny EIP-3009 permit (e.g. 0.10 USDC)
>    against `MockUSDC`, POST it to `POST /api/x402/pay` with `agentId` set,
>    and confirm the returned `sessionId` reads back through
>    `GET /api/session/<sessionId>` as `state=open`. Then cancel via
>    `POST /api/session/<sessionId>/cancel` so I get the USDC back.
>
> 7. **Summary.** Print:
>    - `agentId`
>    - `txHash` for each of the 3 on-chain writes
>    - Snowtrace URLs
>    - Current stake + tier
>    - Marketplace URL `$BASE_URL/agent/<agentId>`
>
> ### Hard rules
> - NEVER hardcode a contract address. Always resolve from env via
>   `onchain.get_deployment()["contracts"]`.
> - NEVER print or commit the private key. If you need to reference it in
>   shell, use `$DEPLOYER_PRIVATE_KEY` from env.
> - NEVER use the Fuji `MockUSDC` faucet on mainnet - it doesn't exist there.
>   If `CHAIN_ID` is 43114, require the wallet to already hold real USDC.
> - NEVER skip the gas balance check; failed txs cost gas too.
> - If any write reverts, DO NOT retry silently. Surface the revert reason
>   and stop.

---

## What the deployer needs to prepare

Before running the prompt against a coding assistant, the deployer should have:

1. A funded Avalanche wallet (AVAX for gas + USDC for stake + listing fee).
2. A `.env` file populated from `.env.example` with:
   - `DEPLOYER_PRIVATE_KEY=0x...`  (their signer)
   - `CHAIN_ID`, `RPC_URL` for the target network
   - Every `*_ADDRESS` from `.env.example` pointing at the deployed contracts
     on that network (the six lines under "On-chain: contract addresses")
3. A reachable HTTPS endpoint for the agent's API, or a cloud agent they have
   already deployed elsewhere.

Once `.env` is populated, `python app.py` runs the backend against their
deployment with zero code changes - the stack is already wired through
`onchain.get_deployment()` and `/config.js`.

---

## What happens on-chain

The prompt runs exactly three signed writes against AgentHire contracts:

1. `AgentRegistry.registerAgent(wallet, name, endpointURL)` - mints the
   agent's identity and emits `AgentRegistered`.
2. `MockUSDC.approve(StakingSlashing, stakeUSDC)` + `StakingSlashing.stake(id,
   stakeUSDC)` - pledges capital that gets slashed on verified incidents.
3. `MockUSDC.approve(EscrowPayment, testAmount)` + an EIP-3009 permit to the
   x402 facilitator - opens a tiny session so the listing is known to be
   payable, then cancels it to return the test deposit.

No admin keys are used. Every write is signed by the deployer's wallet; the
facilitator only gaslessly rebroadcasts the EIP-3009 permit on their behalf.
