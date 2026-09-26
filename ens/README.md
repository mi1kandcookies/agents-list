# Names sidecar (`ens/`)

A small Node HTTP service that issues ENS names for agents and jobs on
**ENSv2, Ethereum Sepolia**. The Flask app calls it through
`app/names/client.py`. It listens on `127.0.0.1:8787` only, and every request must
carry `X-Sidecar-Token: $ENS_SIDECAR_TOKEN`.

It uses plain [viem](https://viem.sh). Its only dependency is `viem`.

```
agentslist-app.eth                         platform-owned root
└─ helper.agentslist-app.eth               agent: agent-context, agent-endpoint[mcp],
   │                                       agent-registration[<ERC-7930 registry>][<agentId>]
   └─ eng-xxxx.helper.agentslist-app.eth   job: sow-hash, escrow, mandate, status, deliverable
      └─ eng-yyyy.eng-xxxx.helper…         sub-job: wildcard records on the job's resolver
```

- **Agents** get their own PermissionedResolver (records are written in its
  initializer) and their own UserRegistry, which holds their jobs. The platform owns
  the name, and the agent gets no roles on it.
- **Jobs** are registered with `expiry` = the SOW deadline. The owner is the platform
  with `roleBitmap = 0`, so the name can't be transferred, and only the platform can
  unregister it. Each job has **its own PermissionedResolver**. On that resolver, the
  hired agent gets `setText` rights for the `status` and `deliverable` keys **only**
  (`grantSetterRoles`). Resolver roles apply per resolver, not per name. A dedicated
  resolver therefore limits the grant to this one job.
- **Sub-jobs** have no registry. They are text records for the full sub-name, written
  on the parent job's resolver. The Universal Resolver serves them by wildcard. They
  expire and revoke together with the job. Sub-agents get no write grants: a grant on
  the job resolver would also cover the parent job's keys.
- **Revoke** (at settlement) revokes the key grants and unregisters the name. For a
  sub-job it clears the records. The name also stops resolving by itself once `expiry`
  passes.
- Record key syntax (ENSIP-25/26, both Draft) lives only in `lib/constants.mjs`
  (`RECORD_KEYS`). Callers send logical names: `context`, `mcp`, `erc8004_agent_id`,
  `sow_hash`, `escrow`, `mandate`, `status` and `deliverable`.

## Endpoints

| Method & path | Body | Notes |
|---|---|---|
| `GET /health` | | mode, operator address, root status, address-check result |
| `POST /names/root/setup` | `{duration_days?}` (default 365, min 28) | one-off, **takes about 70 s** (see below) |
| `POST /names/agent` | `{agent_public_id, label, records}` | |
| `POST /names/job` | `{parent, label, expiry, records, grantee?}` | `expiry` is in unix seconds; `grantee` = the hired agent's address |
| `POST /names/subjob` | `{parent, label, expiry, records}` | expiry is capped at the job's |
| `POST /names/revoke` | `{name, grantee?}` | `grantee` is only needed if the state file was lost |
| `GET /names/tree?root=&live=1` | | `live=1` (live mode) re-reads each text record through the Universal Resolver |

Errors are returned as `{error, code}`. The status is 4xx for bad input, 409 for a
parent that isn't ready or a name that is taken, 503 when the address check failed,
and 502 for chain or RPC failures. Every operation records each step on the name. A
retry after a failure resumes from the failed step and does not deploy again.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `ENS_SIDECAR_TOKEN` | — (required) | shared secret, also set in the Flask app |
| `ENS_ROOT_LABEL` | `agentslist-app` | parent label; the root name is `<label>.eth` |
| `DRY_RUN` | off | `1` = no network: deterministic fake tx hashes, in-memory tree |
| `ENS_OPERATOR_PRIVATE_KEY` | — | platform hot wallet (live only). Use a fresh key and never a personal wallet |
| `SEPOLIA_RPC_URL` | — | Sepolia JSON-RPC endpoint (live only) |
| `ENS_SIDECAR_PORT` | `8787` | |
| `ENS_STATE_FILE` | `.state/names.json` (live), none (dry run) | deployed addresses and progress; git-ignored |

The Flask side reads `ENS_SIDECAR_URL`, `ENS_SIDECAR_TOKEN` and `ENS_ROOT_NAME`
(`agentslist-app.eth`).

## Run it

```bash
cd ens && npm install
npm test                                           # DRY_RUN, no network, CI-safe
DRY_RUN=1 ENS_SIDECAR_TOKEN=dev npm start          # local dry-run sidecar
curl -s -H 'X-Sidecar-Token: dev' localhost:8787/health
```

`npm test` uses `node:test` and runs entirely in DRY_RUN mode. It needs no key, RPC
or network, so it is safe to add to CI next to `pytest`. The current CI workflow does
not run it.

## Address check

The contract addresses come only from the ENS docs *Deployments → Sepolia ENSv2*
table. Several other Sepolia v2 deployments exist, and names created through them
do not resolve through the canonical Universal Resolver. At startup the sidecar
checks the following:
`UniversalResolver(0xeEeE…EeEe).ROOT_REGISTRY()` → root, then
`root.getSubregistry("eth")` = the configured ETHRegistry, and
`ETHRegistrar.ETH_REGISTRY()` returns the same address. It also checks that the
chain id is 11155111. If any of these checks fails, `/health` reports it and every
write returns 503. The ENSv2 beta is redeployed from time to time. In that case,
update `lib/constants.mjs` from the docs table.

## Costs (Sepolia estimates; deploys measured with `eth_estimateGas`)

| Operation | Gas | at ~1–2 gwei |
|---|---|---|
| Root setup (mint, 2 deploys, approve, commit, register, wire-up) | ~1.0–1.2 M | ~0.002 ETH |
| Agent (resolver with records + registry + register) | ~0.9 M | ~0.001–0.002 ETH |
| Job (resolver with records + register + 2 grants) | ~0.8 M | ~0.001–0.002 ETH |
| Sub-job (one multicall) | ~0.1 M | < 0.0005 ETH |
| Revoke a job (2 role revokes + unregister) | ~0.15 M | < 0.0005 ETH |

The root name is paid in **MockUSDC**, which setup mints itself (`mint` is open):
about 8 MockUSDC per year for a label of 5 or more characters. Sepolia gas can spike
past 20 gwei. Budget roughly 10× the figures above.

## Going live (once the operator wallet is funded)

1. Create a **fresh** EOA for the platform. It owns the root name and holds every
   root role on the registries and resolvers.
2. Fund it with **0.3–0.5 Sepolia ETH**. That covers the root plus tens of agents and
   jobs, even during gas spikes. It needs no USDC or other tokens.
3. Start the sidecar in live mode:
   ```bash
   cd ens
   ENS_OPERATOR_PRIVATE_KEY=0x… SEPOLIA_RPC_URL=https://… \
   ENS_SIDECAR_TOKEN=$(openssl rand -hex 24) npm start
   ```
   Check that the startup log does not say `address check FAILED`.
4. Register the root once. The ETHRegistrar requires at least 60 s between commit and
   register, so this call blocks for about 70 s:
   ```bash
   curl -s --max-time 600 -X POST -H "X-Sidecar-Token: $ENS_SIDECAR_TOKEN" \
     -H 'content-type: application/json' -d '{}' localhost:8787/names/root/setup
   ```
5. Point the app at the sidecar: set `ENS_SIDECAR_URL`, `ENS_SIDECAR_TOKEN` and
   `ENS_ROOT_NAME=<label>.eth`. Names left `pending` or `failed` can be retried with
   `POST /api/names/<name>/retry`.
6. Verify one name with any ENS client that uses the canonical Universal Resolver,
   for example `GET /names/tree?live=1`.

Keep `.state/names.json` (git-ignored). It holds the deployed resolver and registry
addresses and the commit secret for an unfinished root registration. If the file is
lost, the sidecar falls back to on-chain lookups (`getSubregistry`/`getResolver`).
