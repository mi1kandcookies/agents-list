# Hosting on Vercel

The web app runs on Vercel's Python runtime as one Vercel Function. Vercel
detects Flask on its own: it finds `wsgi.py` at the repository root and loads
the `app` object from it
([Flask on Vercel](https://vercel.com/docs/frameworks/backend/flask),
[Python entrypoints](https://vercel.com/docs/functions/runtimes/python#python-entrypoints)).
This document covers what the repo already does for that, the steps a person
takes to create the project, and what does not run on Vercel.

## What the repo does

| File | Purpose |
|---|---|
| `wsgi.py` | Entrypoint. Vercel loads `app` from it. Nothing Vercel-specific. |
| `vercel.json` | `buildCommand` copies `app/static` to `public/static`, so `/static/...` is served by Vercel's CDN (Vercel ignores Flask's `static_folder`; files in `public/` are served as-is). `functions["wsgi.py"]` sets `maxDuration: 120` and keeps `public/` and bytecode out of the function bundle. |
| `.vercelignore` | Uploads only the web app. Tests, docs, the names sidecar (`ens/`), the specialist kit (`agentkit/`, `specialists/`), the MCP server, scripts, virtualenvs, `instance/` and `.worktrees/` stay local. |
| `requirements.txt` | What Vercel installs. The provider SDKs (`requirements-agents.txt`), MCP SDK (`requirements-mcp.txt`) and test tools (`requirements-dev.txt`) are separate files and are not installed. |

App behavior when the platform sets `VERCEL=1`:

- **Production config by default.** Without `FLASK_ENV`, the app uses the
  production config: strict validation (`SECRET_KEY`, `API_KEY`, non-wildcard
  `CORS_ORIGINS`), HTTPS-only session cookie, `AUTO_MIGRATE` off.
- **Startup refuses what cannot work on serverless**, whatever `FLASK_ENV`
  says: a SQLite (or missing) `DATABASE_URL`, and a missing
  `MANDATE_SIGNING_KEY` (a key generated into `instance/` would differ on
  every instance, so mandate tokens issued by one would fail on another).
- **Proxy headers are trusted** (`TRUST_PROXY` defaults on). Werkzeug's
  `ProxyFix` takes the scheme, host and client IP from one hop of
  `X-Forwarded-Proto`, `X-Forwarded-Host` and `X-Forwarded-For`. Vercel sets
  all three and overwrites any client-sent `X-Forwarded-For`
  ([request headers](https://vercel.com/docs/headers/request-headers)). This
  makes `url_for(..., _external=True)`, `request.scheme`, the same-origin
  check on `/api/engagements/*` and the rate limiter's client IP correct.
- **Postgres engine options**: `pool_pre_ping`, `pool_recycle=300`, and
  psycopg's `prepare_threshold=None` so a transaction-mode connection pooler
  works.

Session cookie in production: `Secure`, `HttpOnly`, `SameSite=Lax`. World ID
returns to `/auth/world/callback` with a top-level GET redirect (the provider
advertises only `response_modes_supported: ["query"]` in its
[discovery document](https://sandbox.auth.world.org/.well-known/openid-configuration)),
and `Lax` cookies are sent on that navigation.

### State across instances

Serverless requests land on any instance, so nothing that decides an outcome
lives in process memory:

| State | Where it lives |
|---|---|
| Web flow `state`, PKCE verifier, nonce | Approval row in Postgres |
| "This browser started this sign-in" binding | Signed cookie session |
| Device flow `device_code`, poll interval, next poll time | Approval row in Postgres. The client also keeps the interval per process; the stored interval is passed on every poll, so a `slow_down` seen by one instance is honoured by the next. |
| Replay guard (`jti`) | `used_id_token_jtis` table |
| Single execution of an approval | `SELECT ... FOR UPDATE` on the approval row |
| Discovery document, JWKS, USDC domain, screening evidence | Per-process caches. A cold instance refetches them; correctness does not depend on them. |
| Rate-limit counters | Per process (`memory://`). Limits are therefore per instance. Set `RATELIMIT_STORAGE_URI` to a shared store (e.g. `redis://...`) for global limits; the Redis client package is not in `requirements.txt` and would need adding. |

No background threads run in the web app. Device-flow approvals advance when
something asks: each `GET /api/approvals/<id>` polls the provider at most once
and never before the stored next-poll time.

## Step by step

### 1. Create a Postgres database

Any hosted Postgres works; the app needs a connection URL. Options that plug
into Vercel:

- **Neon** from the Vercel Marketplace (Storage tab of the project). It sets
  `DATABASE_URL` (pooled) and `DATABASE_URL_UNPOOLED` on the project.
- **Supabase**, **Prisma Postgres** or others from the Marketplace, or any
  external provider (Railway, Render, RDS, ...). Copy its URL into
  `DATABASE_URL` yourself.

Use the **pooled** URL for the app (`DATABASE_URL`) and the **direct /
unpooled** URL for migrations. `postgres://` and `postgresql://` URLs are
rewritten to the psycopg driver (`postgresql+psycopg://`) automatically.
Keep `sslmode=require` if the provider includes it.

Preview deployments get the same env vars as production unless you scope them.
Give previews their own database (or a Neon branch) if they should not touch
production data.

### 2. Run migrations (from your machine, before deploying)

Migrations are not run by the build or at boot. A build step would migrate
the database from every preview build, and boot-time migration would race
across cold starts. Run them once per schema change, locally, against the
direct URL:

```bash
. .venv/bin/activate
DATABASE_URL='postgresql://USER:PASSWORD@HOST/DB?sslmode=require' \
FLASK_ENV=development AUTO_MIGRATE=0 \
flask --app wsgi db upgrade
```

(`FLASK_ENV=development` only skips the production checks for this one CLI
run; the command talks to the database named in `DATABASE_URL`.) Optional
sample listings: `flask --app wsgi seed` with the same variables.

Order for a change that adds a migration: merge, run `db upgrade` against the
hosted database, then let the deployment go live. The running deployment
keeps serving on the new schema in between, so a migration that is not
backward compatible (dropping or renaming a column the old code reads) needs
to be split across two deploys.

### 3. Create the Vercel project

1. Vercel dashboard → **Add New → Project** → import the GitHub repository.
2. Framework preset: **Flask** (auto-detected). Leave the build command,
   output directory and install command empty; `vercel.json` supplies the
   build command.
3. Python version: default (3.12). The app is tested on 3.12-3.14.
4. Add the environment variables below (Settings → Environment Variables),
   then deploy.
5. Settings → Domains: add the production domain. The World ID callback URL
   is built from it.

### 4. Environment variables

Never put real values in the repo. `VERCEL`, `VERCEL_ENV` and `VERCEL_URL`
are set by the platform.

**Core**

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | required | Long random string; signs the session cookie. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | required | Pooled Postgres URL. SQLite is refused. |
| `MANDATE_SIGNING_KEY` | required | ES256 (P-256) private key PEM, the same on every instance. Generate: `python -c "from app.mandates.tokens import generate_pem; print(generate_pem())"`. Paste as a multi-line value, or with literal `\n` between lines. |
| `API_KEY` | required | Protects `/admin/*` and seller mutations. Production validation fails without it. |
| `CORS_ORIGINS` | required | Comma-separated origins for `/api/*`, e.g. `https://agents.example`. Wildcard is refused in production. |
| `MCP_API_TOKEN` | recommended | Bearer token for non-browser clients of `/api/engagements/*` (the MCP server, scripts). Without it those endpoints are open. |
| `FLASK_ENV` | optional | Defaults to `production` on Vercel. |
| `STRICT_PROD_VALIDATION` | optional | Default `1`. |
| `TRUST_PROXY` | optional | Default `1` on Vercel. |
| `SESSION_COOKIE_SECURE` | optional | Default `1` in production. |
| `RATELIMIT_DEFAULT` / `RATELIMIT_STORAGE_URI` | optional | `600/minute`, `memory://` (per instance). |
| `AUTO_MIGRATE` | optional | Leave unset (off). |

**World ID**

| Variable | Required | Notes |
|---|---|---|
| `WORLD_CLIENT_ID` | required for sign-in and approvals | From the developer portal. |
| `WORLD_CLIENT_SECRET` | required for sign-in and approvals | Shown once in the portal; paste it straight into Vercel. |
| `WORLD_REDIRECT_URI` | required for production | `https://<domain>/auth/world/callback`, exactly as registered. |
| `WORLD_ALLOWED_HOSTS` | optional | For previews: leave `WORLD_REDIRECT_URI` unset in the Preview environment and list preview hosts here (comma-separated, exact, e.g. `agents-git-staging-team.vercel.app`). A request on a listed host uses `https://<host>/auth/world/callback`; any other host cannot start a web sign-in. Each host must also be registered in the portal. |
| `WORLD_ISSUER` | optional | Default `https://sandbox.auth.world.org`. |
| `WORLD_REQUIRED_ACR` | optional | e.g. `https://world.org/oidc/acr/orb-v3`. |
| `APPROVAL_TTL_SECONDS` / `STEPUP_MAX_AGE_SECONDS` / `HUMAN_WEEKLY_CAP_USDC` | optional | Defaults 180 / 300 / built-in cap. |

**Screening**

| Variable | Required | Notes |
|---|---|---|
| `INTERCEPTA_API_KEY` | required for payments | Without it every payment is refused (fail closed). |
| `INTERCEPTA_BASE_URL` | optional | Default provider URL. |
| `SCREENING_ADDRESS_MAP` | optional | Inline JSON on Vercel (a file path must be a file in the deployment). |
| `SCREENING_TIMEOUT_SECONDS`, `SCREENING_REFUSE_SCORE`, `SCREENING_ASK_SCORE`, `SCREENING_CAP_SCORE`, `SCREENING_CAP_USDC` | optional | See `.env.example`. |

**Chain (Sepolia), escrow and payments**

| Variable | Required | Notes |
|---|---|---|
| `RPC_URL` | recommended | Keyed Sepolia RPC; the public default is rate-limited. |
| `ESCROW_PRIVATE_KEY` | optional | Unset: ledger entries are simulated. |
| `BUYER_VAULT_PRIVATE_KEY` | optional | Buyer vault for EIP-3009 funding authorizations. |
| `FACILITATOR_PRIVATE_KEY` | optional | Pays gas for x402 settlements. |
| `GATEKEEPER_PRIVATE_KEY` | optional | Legacy reputation contract only. |
| `PAYMENT_RECIPIENT` | optional | Treasury address. |
| `CHAIN_ID`, `CHAIN_NAME`, `EXPLORER_URL`, `RPC_TIMEOUT_SECONDS`, `USDC_ADDRESS`, `DOMAIN_CACHE_SECONDS`, `MIN_SIGNER_ETH_RESERVE`, `PRIORITY_FEE_GWEI`, `MAX_FEE_GWEI`, `ERC8004_IDENTITY_REGISTRY`, `ERC8004_REPUTATION_REGISTRY`, `AGENT_REGISTRY_ADDRESS`, `REPUTATION_ADDRESS`, `ESCROW_ADDRESS`, `STAKING_ADDRESS` | optional | Sepolia defaults; see `.env.example`. |
| `MANDATE_MAX_DEPTH`, `AGENT_TASK_PRICE_USDC`, `X402_MAX_PAYMENT_USDC` | optional | See `.env.example`. |

**Names (ENS)**

| Variable | Required | Notes |
|---|---|---|
| `ENS_SIDECAR_URL` / `ENS_SIDECAR_TOKEN` | optional | Base URL and shared secret of a names sidecar hosted elsewhere (see below). Unset: names stay "pending". |
| `ENS_SIDECAR_TIMEOUT`, `ENS_ROOT_NAME`, `ENS_RESOLVE_PAYEES`, `ENS_UNIVERSAL_RESOLVER` | optional | Resolution reads through `RPC_URL` and works on Vercel. |

**Other**

| Variable | Required | Notes |
|---|---|---|
| `LLM_URL`, `LLM_MODEL`, `LLM_API_KEY` | optional | OpenAI-compatible endpoint for SOW parsing and previews. |
| `TOKEN_CALIBRATION_PATH` | optional | Must point at a file inside the deployment. |

### 5. Register the callback in the World ID developer portal

The sandbox issuer's portal is <https://sandbox.auth.world.org/portal>
(sign in with Google; portal access can be limited by account eligibility,
see the `getting-started` guide below). Per the provider's integration guide
(`oidc`):

1. **Create an OIDC client** (or open the existing one).
2. **Redirect URI**: `https://<your-domain>/auth/world/callback`. Callbacks
   match exactly (scheme, host, path, port); there are no wildcards. Sandbox
   callbacks must be HTTPS.
3. **Client authentication**: `client_secret_basic` (what the app sends).
   The method is immutable once registered.
4. **Save the secret** straight into `WORLD_CLIENT_SECRET` in Vercel; it is
   shown only once.
5. **Device grant**: the sandbox discovery document advertises
   `urn:ietf:params:oauth:grant-type:device_code` next to
   `authorization_code`, and the guide says "a device-only client still needs
   a registered redirect URI in the portal, although the device grant does not
   use it". The guide describes no separate switch for it; if the portal shows
   a grant-type option for the client, enable the device code grant. Verify
   with one device approval (`python scripts/world_smoke.py` with the client
   credentials set).
6. **Preview hosts (optional)**: add each preview callback, e.g.
   `https://agents-git-staging-team.vercel.app/auth/world/callback`, and list
   those hosts in `WORLD_ALLOWED_HOSTS` for the Preview environment. Use
   stable branch aliases; per-commit deployment URLs change every push and
   cannot be pre-registered.

**The pairwise sector.** "Portal registrations use the redirect hostname as
the immutable sector." The `sub` a human gets is per sector, so a client
registered with `127.0.0.1` callbacks gives different `sub` values than one
registered for the production domain. Callbacks on several hostnames in one
client need a published HTTPS `sector_identifier_uri` (a JSON array of every
exact redirect URI). Simplest: register a **separate client for the hosted
site** (production domain first, so it becomes the sector) and keep the
local-development client as it is. Humans who signed in locally will be new
humans on the hosted site.

Source for the quotes above: the provider's public guides, served by its MCP
endpoint `https://sandbox.auth.world.org/mcp` (`list_idp_guides`, then
`get_idp_guide` with `getting-started` and `oidc`; no sign-in required, per
<https://sandbox.auth.world.org/llms.txt>), and the overview at
<https://sandbox.auth.world.org/docs>.

### 6. What "works for anyone" means on the sandbox issuer

The site itself needs nothing per visitor: any visitor can start a web
sign-in or an approval, and device-flow codes can be approved from any
device. What each visitor needs is a way to complete the World ID proof on
the sandbox issuer:

- The provider handles the proof: its page shows a QR code / link and waits
  for "World ID app" confirmation ("Scan the QR code or use the secure link
  ... Approve the request in World ID app"). The sandbox front end accepts
  connector links on `https://*.world.org/verify` or the `worldidsandbox://`
  and `worldidstg://` schemes (observed in the sandbox's web bundle, not a
  documented contract).
- The `oidc` guide: "The current compatibility flow requires a legacy-capable
  World ID 3.0 Orb credential and a compatible World ID app.
  `world_id_3_not_available` means that this flow cannot authenticate that
  credential." The `getting-started` guide adds: "Test proof behavior in a
  non-production environment is not evidence of production verification."
- The public docs do not say which app build completes a **sandbox** proof
  (the production World App, a sandbox/staging build, or the web simulator
  at <https://simulator.worldcoin.org>, which World documents for its older
  staging environment: <https://docs.world.org/world-id/quick-start/testing>).
  Test this with a device that is not a developer's before relying on it,
  and ask World if it fails. Until then, assume a visitor needs a World ID
  app that can complete sandbox proofs.
- The device grant always requires a fresh proof and explicit approval in
  the app; approving creates no browser session on the provider.
- Moving to the production issuer later means a new client in the production
  portal, a new `WORLD_ISSUER`, and new `sub` values for every human.

## What does not run on Vercel

| Component | Status on Vercel | What to do |
|---|---|---|
| Names sidecar (`ens/`, Node) | Not deployed (excluded by `.vercelignore`). | Run it on a small always-on host (a VM or container service) with its own Sepolia key, and set `ENS_SIDECAR_URL` / `ENS_SIDECAR_TOKEN`. Or leave it unset: names stay "pending" and can be retried later. |
| MCP server (`agentslist_mcp/`) | Runs on the user's machine (stdio). | Point it at the hosted site: `MCP_API_BASE=https://<domain>` and `MCP_API_TOKEN=<same value as on Vercel>`. See `docs/mcp.md`. |
| Specialist kit (`agentkit/`, `specialists/`) | Not part of the web app. | Runs where specialists run (a VM), unchanged. |
| Migrations | Not run by build or boot. | Step 2 above. |
| Local disk | Read-only apart from `/tmp`, and not shared. | The app writes nothing at runtime on Vercel: statements of work are parsed in memory, and the only disk write (a development mandate key in `instance/`) is replaced by the required `MANDATE_SIGNING_KEY`. |
| mkcert / local HTTPS | Not needed. | Vercel terminates HTTPS. `DEMO_CHECK_CA_BUNDLE` in `scripts/demo_check.py` is only for local mkcert certificates; against the hosted site run `python scripts/demo_check.py https://<domain>`. |

### Timeouts

`maxDuration` is 120 s (Vercel's default is 300 s on every plan with Fluid
compute: [duration limits](https://vercel.com/docs/functions/configuring-functions/duration)).
Outbound calls are bounded individually:

| Call | Timeout |
|---|---|
| World ID discovery, JWKS, token, device endpoints | 10 s each. A callback on a cold instance can make three calls (discovery, token, JWKS). |
| Intercepta screening | `SCREENING_TIMEOUT_SECONDS` (4 s); a timeout refuses the payment. |
| Sepolia RPC | `RPC_TIMEOUT_SECONDS` (10 s) per call. |
| Names sidecar | `ENS_SIDECAR_TIMEOUT` (15 s). |
| LLM (SOW parsing) | 60 s. |

A web approval callback that also executes a payment chains several of these
(token exchange, re-screen, RPC); the sum stays well under 120 s.

### Other limits

- Request bodies are limited to 4.5 MB on Vercel Functions, below the app's
  5 MB statement-of-work limit; larger uploads get a platform 413.
- The function bundle limit is 500 MB; `requirements.txt` (Flask, SQLAlchemy,
  web3, x402, psycopg binary, pypdf, python-docx) is well under it.
