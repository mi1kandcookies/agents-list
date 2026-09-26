# World ID

Agent's List uses World ID as its OpenID Connect provider for humans. Every
money-moving action an agent requests (funding an engagement, releasing a
milestone, sub-hiring) waits for a fresh approval from the human who owns it.
World ID gives each relying party a stable, private `sub` per human (pairwise
subject), so the marketplace can recognize the same person across devices and
agents without learning who they are.

Client code: `app/identity/world.py` (`WorldClient`) and `app/identity/pkce.py`.

## Flows

- **Web** — authorization code with PKCE (`S256`). Every request sends
  `prompt=login` and `max_age=0` so the human authenticates again, plus a
  `state` and a `nonce` derived from the exact action being approved.
- **Device** (RFC 8628) — for agents and CLIs that have no browser. The backend
  starts a device authorization and shows the `user_code` /
  `verification_uri_complete`; the human approves on their own device while the
  backend polls the token endpoint. `authorization_pending` keeps polling,
  `slow_down` adds 5 s to the interval, `access_denied` and `expired_token` end
  the approval.

The token endpoint is authenticated with `client_secret_basic`. Endpoints come
from the provider's discovery document (cached for one hour).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WORLD_ISSUER` | `https://sandbox.auth.world.org` | Provider base URL |
| `WORLD_CLIENT_ID` / `WORLD_CLIENT_SECRET` | — | OIDC client credentials |
| `WORLD_REDIRECT_URI` | — | Web-flow callback (`/auth/world/callback`) |
| `WORLD_ALLOWED_HOSTS` | unset | Used only when `WORLD_REDIRECT_URI` is unset: a request to one of these exact hosts uses `https://<host>/auth/world/callback`. Other hosts cannot start a web sign-in. See `docs/deploy/vercel.md`. |
| `WORLD_REQUIRED_ACR` | unset | If set, ID tokens must carry exactly this `acr`, e.g. `https://world.org/oidc/acr/orb-v3` |

`python scripts/world_smoke.py` prints the live discovery document and, with
client credentials set, runs one device approval end to end.

## Validation rules

An ID token is accepted only if every check below passes. Each failure has a
stable code so approvals record exactly why they were rejected.

| Check | Rule | Code |
|---|---|---|
| Algorithm | Header `alg` must be `RS256`. `none`, HMAC (`HS256` signed with the public key) and anything else are refused before any key is used. The matching JWK must be an RSA signing key. | `BAD_ALG` |
| Signature | Verified against the provider JWKS key named by the header `kid`. An unknown `kid` triggers one JWKS refetch (key rotation), rate-limited to once a minute. Malformed tokens and tokens without a `kid` fail here too. | `BAD_SIGNATURE` |
| Required claims | `iss`, `sub`, `aud`, `exp`, `iat` present; `exp`/`iat` integers. `auth_time` is required whenever freshness is checked. | `MISSING_CLAIM` |
| Issuer | `iss` equals the discovery `issuer`, which itself must equal the configured `WORLD_ISSUER`. | `BAD_ISSUER` |
| Audience | `aud` contains our client id. With several audiences, or whenever `azp` is present, `azp` must equal our client id. | `BAD_AUDIENCE` |
| Lifetime | Not past `exp`, and `iat` not in the future, each with 60 s clock leeway. | `EXPIRED` |
| Action binding | `nonce` equals the nonce derived from the action being approved (constant-time compare). A device flow started without a nonce skips this check; the approval is then bound server-side by its device code. | `NONCE_MISMATCH` |
| Freshness | `auth_time` is no earlier than the start of this approval (5 s skew), and no older than the allowed age (`STEPUP_MAX_AGE_SECONDS`). A cached session cannot approve a new payment. | `STALE_AUTH` |
| Assurance level | When `WORLD_REQUIRED_ACR` is set (or the caller asks for a specific level), `acr` must match exactly. | `ACR_MISMATCH` |

Token replay (`jti` reuse) is checked by the approval service, which stores
every accepted `jti`.
