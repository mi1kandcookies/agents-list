# Local Sepolia wallet roles

The local test setup uses five separate Ethereum Sepolia EOAs. Their private
keys are stored in the macOS Keychain under the `agents-list` account and are
never committed, printed, or written to `.env`.

Show the public addresses:

```bash
python scripts/show_local_wallets.py
```

Run a command with the role-specific keys loaded into its environment:

```bash
scripts/keychain_env.sh ./\.venv/bin/flask --app wsgi run --port 8090
scripts/keychain_env.sh npm --prefix ens start
```

The roles are intentionally separate:

- `ens-operator`: owns the ENSv2 root and resolver operations.
- `facilitator`: pays gas to submit EIP-3009 authorizations.
- `escrow`: receives and releases the server-side escrow balance.
- `buyer-vault`: signs approved funding authorizations.
- `x402-payer`: signs the specialist task payment.

The launcher also loads the shared `agents-list.sidecar-token` Keychain entry
as `ENS_SIDECAR_TOKEN`. It does not enable live writes by itself. The ENS
sidecar must be started without `DRY_RUN=1` and the explicit setup command must
be used before any name-registration transaction is sent.
