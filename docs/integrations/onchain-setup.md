# On-chain setup

The application targets Ethereum Sepolia (`11155111`). The default payment
asset remains Circle USDC on Sepolia. Wallet roles are separate so a buyer
signature, gas sponsorship, ENS administration and escrow custody are not
combined in one key.

## Development payment token

`contracts/MockUSDC3009.sol` is a development-only ERC-20 with the EIP-3009
`transferWithAuthorization` and `receiveWithAuthorization` methods used by the
payment adapters. It has no owner: anyone can mint, and an account can burn its
own balance. That makes local and Sepolia integration tests reproducible, but
it is not a real asset and must never be used for production funds.

The application stays on Circle USDC unless the mock is explicitly selected:

```dotenv
PAYMENT_TOKEN_MODE=mock
MOCK_USDC_ADDRESS=0x<deployed MockUSDC3009 address>
```

In mock mode, `chain.config.get_address("USDC")` returns
`MOCK_USDC_ADDRESS`, so the existing EIP-712 domain discovery, x402 exact
requirements, escrow adapter and token allowlist all use the same configured
address. Without that opt-in, the configured token is Circle USDC.

The current Sepolia demo deployment is
`0xD6211813CF03cb2cF8e2c848E3D0994063b0B5f1`:
[contract](https://sepolia.etherscan.io/address/0xD6211813CF03cb2cF8e2c848E3D0994063b0B5f1)
· [deployment transaction](https://sepolia.etherscan.io/tx/0xcf7626cd771ccaafeb9577d298949657f0601f4ea48abb86c5a234a517578111).
It is intentionally a demo token and is not part of the production asset
configuration.

Build and unit-test the contract locally:

```bash
forge build
forge test -vv
```

Deploying is deliberately a separate, explicit action. Load the deployer key
from a local secret manager, set `RPC_URL` to Ethereum Sepolia, and run
`scripts/deploy_mockusdc.sh`. The checked-in script is the reproducible path;
it does not run during application startup or tests.

Before funding or deploying, run the read-only preflight:

```bash
python scripts/onchain_preflight.py
```

## Wallet roles

Use one fresh test-only wallet per role:

| Role | Purpose | Needed asset |
|---|---|---|
| ENS operator | Registers and updates the ENSv2 namespace | Sepolia ETH |
| Facilitator | Submits buyer-signed EIP-3009 authorizations | Sepolia ETH |
| Buyer vault | Signs escrow funding authorizations | ETH only for key custody; mock/USDC balance |
| x402 payer | Signs specialist task payments | ETH only for key custody; mock/USDC balance |
| Escrow wallet | Holds funds between funding and release | Sepolia ETH for releases; mock/USDC balance |
| Mock deployer | Deploys the development token | Sepolia ETH |

The repository never stores private keys. Use the local keychain launcher or a
`0600` untracked environment file for development only. Public addresses may be
shared for funding; private material must not be committed, logged or supplied
to an agent model.

## Current protocol boundary

ENS uses the deployed ENSv2 Sepolia contracts through the Node sidecar. The
sidecar's root setup and name writes are explicit live operations and are not
run by tests. The payment path currently uses the existing EIP-3009 facilitator
and server-side escrow ledger; no custom escrow contract is assumed to be
deployed. A live Sepolia fund/release smoke has been confirmed with the demo
token ([fund transaction](https://sepolia.etherscan.io/tx/0xddb3937d6be20c6593e9448ab51a6cb423a0233a7bf5740ca7ee409c2be22726),
[release transaction](https://sepolia.etherscan.io/tx/0x2de41376d1c91c78c3f89e12f9c75c0d1d7e8f2282229914b82edb5383426645)).
The World approval client remains the identity boundary: a valid approval is
required before an application payment attempt is claimed or signed.
