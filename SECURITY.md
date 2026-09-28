# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: **[Report a vulnerability](https://github.com/mi1kandcookies/agents-list/security/advisories/new)** (Security tab → "Report a vulnerability"). Include steps to reproduce, the affected component and the impact you expect.

We aim to acknowledge reports within 3 business days and to share a fix plan or decision within 10 business days.

## Scope

In scope: this repository's code, including the web app and API, approval and escrow logic, payment screening, mandate tokens, the names service under `ens/`, and the MCP server.

Out of scope: third-party services the app integrates with (identity provider, screening provider, RPC and model hosts), findings that need a compromised maintainer machine, and denial-of-service by volume.

## Deployments

The project runs on Ethereum Sepolia with test tokens only. Never send mainnet funds to any address in this repository. If you find a committed credential, report it privately; do not use it.
