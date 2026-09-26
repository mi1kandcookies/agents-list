# 0001 — Chain of custody: interface contracts

Status: accepted. These contracts are frozen; changes go through a small amendment PR that updates this file first.

Goal: every dollar an agent moves (including when it sub-hires other agents) traces back through a narrowing chain of scoped authority to one fresh, verified human approval of that exact action.

## 1. Canonical action hash — `app/approvals/actions.py`

```
canonical(obj) = UTF-8 JSON, keys sorted, separators (",", ":"), ensure_ascii=False
                 integers only (money in micro-USDC); floats raise TypeError
                 addresses lowercased 0x-hex; agent ids as AGT-XXXX-XXXX-C; timestamps int unix seconds
action_hash    = "0x" + sha256(canonical(action)).hexdigest()
action_nonce   = base64url_nopad(sha256(canonical(action)))     # 43 chars, sent as the OIDC nonce
sow_hash       = "0x" + sha256(canonical(sow)).hexdigest()
```

Action object `v1` (every action carries `approval_id` and `exp`, so each hash/nonce is single-use):

```json
{
  "v": 1,
  "kind": "engagement.fund | milestone.release | manifest.publish | subhire.fund | session.login",
  "approval_id": "APR-…",
  "exp": 1790000000,
  "engagement_id": "ENG-…",
  "sow_hash": "0x…",
  "amount_micro": 25000000,
  "payee_agent_id": "AGT-…",
  "payee_address": "0x…",
  "payee_source": "ens | profile",
  "milestones": [{"idx": 0, "amount_micro": 10000000, "title_hash": "0x…"}],
  "milestone_idx": 1,
  "parent_mandate_id": "MND-…",
  "manifest_hash": "0x…",
  "screening_id": "SCR-…",
  "payer_screening_id": "SCR-…",
  "screening_ack": true
}
```
Fields not relevant to a kind are omitted (not null). `payee_source` (amendment) records whether `payee_address` was confirmed by the agent's ENS payout record (`ens`) or taken from the profile (`profile`); see `resolve_payee` in `app/names/service.py`. `payer_screening_id` is present when the payer identity boundary has supplied a wallet, and is evaluated alongside `screening_id`. Functions: `build_action(kind, **fields) -> dict`, `canonical(obj) -> bytes`, `action_hash(obj) -> str`, `action_nonce(obj) -> str`, `sow_hash(sow) -> str`, `describe(action) -> list[tuple[str, str]]` (human-readable summary rows). Golden vectors live in `tests/fixtures/action_vectors.json`.

## 2. World identity client — `app/identity/world.py`

```python
class WorldClient:
    def __init__(self, issuer=None, client_id=None, client_secret=None, redirect_uri=None, http=requests): ...
    def discovery(self) -> dict                      # cached 1h
    def jwks(self) -> dict                           # cached; one refetch on unknown kid
    def authorize_url(self, *, state, nonce, code_challenge, prompt="login", max_age=0, acr_values=None) -> str
    def exchange_code(self, code, code_verifier) -> dict
    def device_authorize(self, *, nonce=None, scope="openid") -> DeviceStart
    def poll_device(self, device_code) -> DevicePoll
    def validate_id_token(self, id_token, *, expected_nonce, not_before, max_age_s, require_acr=None) -> IdClaims

DeviceStart(device_code, user_code, verification_uri, verification_uri_complete, expires_in, interval)
DevicePoll(status: "pending"|"slow_down"|"approved"|"denied"|"expired"|"error", id_token, error)
IdClaims(sub, iss, aud, exp, iat, jti, nonce, auth_time, acr, amr)
IdTokenError.code ∈ {BAD_SIGNATURE, BAD_ALG, BAD_ISSUER, BAD_AUDIENCE, EXPIRED, NONCE_MISMATCH, STALE_AUTH, ACR_MISMATCH, MISSING_CLAIM}
```
Validation: RS256 only against JWKS `kid`; `iss` == discovery issuer; `aud` contains client_id (if multiple, `azp` == client_id); `exp`/`iat` with 60 s leeway; `nonce` == expected when given (device flow without nonce → server-side binding stands, event `nonce_absent`); `auth_time >= not_before - 5` and `now - auth_time <= max_age_s`; `acr` == `WORLD_REQUIRED_ACR` when set. Token endpoint auth `client_secret_basic`. Device errors: `authorization_pending→pending`, `slow_down→` interval+5, `access_denied→denied`, `expired_token→expired`. Web flow always sends `prompt=login`, `max_age=0`, `nonce`, `state`, PKCE S256.

## 3. Approval state machine — `app/approvals/service.py`

```
created ─start─▶ pending ─valid id_token─▶ approved ─consume+execute─▶ consumed
                   ├─ access_denied ─▶ denied        (terminal)
                   ├─ past expires_at / expired_token ─▶ expired (terminal)
                   ├─ cancel ─▶ cancelled             (terminal)
                   └─ validation failure ─▶ rejected  (terminal; failure_code)
created ─ screening REFUSE / banned / cap ─▶ blocked (terminal; never sent to the identity provider)
approved ─ executor error ─▶ failed (terminal; no retry with the same approval)
```
Every transition writes an `approval_events` row. `consume()` runs in one DB transaction: re-select row; require `state == approved` and `now <= expires_at`; recompute hash from `action_json` == `action_hash`; human not banned; weekly cap (fund, subhire); `human_sub` == engagement's claimed buyer (claimed at first approval); fresh re-screen not REFUSE; set `consumed_at`; call executor. Second consume → `ApprovalError("APPROVAL_CONSUMED")`, event `replay_blocked`, HTTP 409. `id_token.jti` stored in `used_id_token_jtis`; duplicate → `rejected` / `REPLAYED_TOKEN`. When a callback or poll reaches `approved`, the executor runs automatically.
(Amendment) `consume()` also runs the kind's `before_consume` checks with the other re-checks; for `engagement.fund` that is `assert_hireable(agent)`, so an agent un-stamped, edited or whose operator was banned while the approval was open ends it `blocked` with that code (`NOT_STAMPED`, `RESTAMP_REQUIRED`, `OPERATOR_BANNED`, `PAYEE_REFUSED`). When an approval ends `denied`/`expired`/`cancelled`/`rejected`/`blocked`, the kind's `on_terminal` hooks run in the same transaction: an `engagement.fund` job goes back from `awaiting_approval` to `scoped` (unless another fund approval for it is still open), and a `subhire.fund` child goes to `cancelled` (cancel) or `refused` (otherwise).

```python
create_approval(kind, action, *, flow, engagement_id=None, milestone_id=None, agent_id=None, screening_id=None) -> Approval
start_web(approval) -> str
complete_web(state, code, error) -> Approval
start_device(approval) -> Approval
poll(approval) -> Approval
cancel(approval_id, reason) -> Approval
consume(approval_id, *, kind) -> ExecutionResult
ApprovalError.code ∈ {NOT_APPROVED, EXPIRED, APPROVAL_CONSUMED, HASH_MISMATCH, WRONG_HUMAN, BANNED, CAP_EXCEEDED, SCREENING_REFUSED}
```
Executor registry — `app/approvals/executors.py`:
```python
EXECUTORS: dict[str, Callable[[Approval, dict], ExecutionResult]] = {}
def executor(kind): ...   # decorator
@dataclass ExecutionResult: ok: bool; summary: str; ledger_ids: list[str]; redirect: str | None
```
Routes: `GET /login`, `GET /auth/world/callback`, `GET /approvals/<id>`, `GET /approvals/<id>/start`, `GET /api/approvals/<id>` (polls device flows), `POST /api/approvals/<id>/cancel`.
Config: `APPROVAL_TTL_SECONDS=180`, `STEPUP_MAX_AGE_SECONDS=300`.

## 4. Screening verdict — `app/screening/`

```python
screen(hop, *, chain_address, amount_micro, engagement_id=None, agent_id=None, typed_data=None) -> Verdict
# hop ∈ {"payee.onboard","payer.check","engagement.fund","milestone.release","subhire.hop","signature"}
```
```json
{
  "id": "SCR-…", "hop": "milestone.release",
  "subject": {"chain_address": "0x…", "screened_address": "0x…", "network": "eip155:1", "agent_id": "AGT-…"},
  "verdict": "PAY | CAP | REFUSE | ASK_HUMAN",
  "cap_micro": null,
  "reasons": [{"code": "TOXIC_SCORE_HIGH", "message": "Toxic score 72 ≥ 60", "source": "quick-scan"}],
  "signals": {"toxic_score": 72, "traits": [], "risk_group": null, "token_risks": []},
  "provider": "intercepta", "fail_closed": false, "latency_ms": 412,
  "created_at": 0, "expires_at": 0
}
```
Default policy: error / timeout (`SCREENING_TIMEOUT_SECONDS=4`) / missing key / unmapped address → REFUSE, `fail_closed=true`. Blocklisted trait or score ≥ 80 → REFUSE. 60–79 or signature riskGroup high/critical → ASK_HUMAN. 30–59 → CAP (`SCREENING_CAP_USDC`, default 10). Otherwise PAY. Screening runs when the action is created (verdict id bound into the action) and again in `consume()` immediately before sending. Agent-to-agent hops screen the payer and payee; fund/release actions screen the World-bound `engagements.buyer_address` when the identity boundary has established it. Sepolia addresses are screened as mapped mainnet addresses (`agents.screening_address`, `SCREENING_ADDRESS_MAP`).

## 5. Mandate token — `app/mandates/tokens.py`

JWS `{"alg":"ES256","typ":"mandate+jwt","kid":…}`, key from `MANDATE_SIGNING_KEY` (dev: generated into `instance/mandate_key.pem`).
```json
{"iss":"agents-list","jti":"MND-…","sub":"AGT-…","hum":"<sha256(world_sub)>",
 "root":"MND-…","par":null,"dep":0,"apr":"APR-…","eng":"ENG-…","sow":"0x…",
 "cap":{"budget_micro":25000000,"categories":["Development"],"max_depth":2,"per_tx_max_micro":10000000,"payees":null},
 "iat":0,"nbf":0,"exp":0}
```
Attenuation (`attenuate(parent_token, child_agent, budget_micro, categories, exp)`, re-checked by `verify_chain`): child budget ≤ parent budget − spent − active siblings; categories ⊆ parent; exp ≤ parent; depth+1 ≤ root max_depth; per_tx_max ≤ parent; root approval consumed and of kind `engagement.fund`; no ancestor revoked/expired; human not banned. Sub-hire calls use `Authorization: Mandate <jwt>`. `chain_graph(engagement_id)` → `{nodes, edges}`.

## 6. Escrow service — `chain/escrow.py` (no Flask imports)

```python
class EscrowService:
    mode: "onchain" | "simulated"     # onchain iff ESCROW_PRIVATE_KEY + facilitator + vault configured
    def fund_from_vault(self, *, amount_micro, valid_seconds=600) -> TxResult
    def fund_from_permit(self, permit) -> TxResult
    def release(self, *, to, amount_micro) -> TxResult
    def receipt_status(self, tx_hash) -> "pending" | "confirmed" | "failed"
TxResult(tx_hash, status, explorer)
```
Fund: vault signs EIP-3009 to the escrow address, submitted via the existing facilitator path. Release: escrow key calls USDC `transfer`. Sub-hire allocation is ledger-only. Receipts are polled asynchronously. Without keys, entries are `simulated` and labeled in the UI.

The paying-agent x402 path uses the official Python SDK's exact EVM client
through `chain/x402_official.py`. Its pre-payment lifecycle hook binds the
selected requirements to the approved terms, and a guarded signer submits the
exact typed authorization to the risk gate before signing. The resource-side
route still performs the local mandate, nonce, screening and escrow checks
before accepting the SDK-compatible payload. The first 402 response also
creates a server-owned `HireIntent` with the named specialist, resolved ENS
payee, endpoint, task hash, integer amount, token, network and expiry. The
intent hash is advertised in x402 `extensions.hireIntent` and `X-HIRE-INTENT`;
the paid request must reference that intent, and the route retrieves the task
from the stored row rather than trusting replacement request text. Only one
attempt can atomically claim an intent; pending settlement stays pending and
is reconciled from the ledger.

## 7. JSON API (used by the MCP server)

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/agents?q=&category=&limit=` | | `[{agent_id, name, category, verified, price_hint_usdc, manifest_hash, stamped, ens_name}]` |
| `POST /api/sow/parse` | multipart `file` (.pdf/.docx/.txt/.md, ≤ 5 MB) or `{text}` | 200 draft scope `{source:{filename, sha256, pages}, outcome, brief, category_key, milestones:[{title, acceptance:[…], amount_usdc\|null}], deadline:{date, mode}, budget_usdc, warnings, notes}` (amendment) |
| `POST /api/engagements` | `{agent_id, outcome, budget_usdc, milestones?:[{title, acceptance, amount_usdc}], deadline?, source_document?:{filename, sha256}}` | 201 engagement (SOW, sow_hash, milestones, screening preview) |
| `POST /api/engagements/<id>/hire` | `{flow:"device"\|"web", confirm_amount_usdc}` | 202 approval |
| `POST /api/engagements/<id>/milestones/<idx>/submit` | `{evidence}` | 200 |
| `POST /api/engagements/<id>/milestones/<idx>/release` | `{flow}` | 202 approval |
| `GET /api/engagements/<id>` | | engagement + milestones, ledger (explorer links), approvals, mandate, chain_url |
| `POST /api/engagements/<id>/subhire` (`Authorization: Mandate …`) | `{agent_id, outcome, budget_usdc, category}` | 201 child engagement or 202 approval (ASK_HUMAN) |
| `GET /api/engagements/<id>/chain` | | `{nodes, edges}` |
| `POST /api/agents/<AGT>/tasks` | first request `{task}`; paid retry `{intent_id}` with `X-HIRE-INTENT`, `X-PAYMENT`/`PAYMENT-SIGNATURE`, and `Authorization: Mandate …` | 402 x402 v2 requirements plus `extensions.hireIntent`; 200 task receipt only when the immutable intent, mandate, payer/payee screening, payment, and settlement all pass. The paid route never accepts replacement task text. |
| `GET /api/approvals/<id>`, `POST …/cancel` | | approval |

Amendment (uploaded SOW): `/api/sow/parse` reads the document in memory and keeps only its filename and SHA-256 (the file is never stored). It returns a draft for the buyer to review, not a contract. When an engagement is created from it, `source_document: {filename, sha256}` is copied into the SOW object, so `sow_hash`, and with it every approval's action binding (§1), covers the exact file the scope came from. SOWs without an upload omit the key and hash as before. Same authentication as the other endpoints here.

Approval object: `{approval_id, kind, state, flow, user_code, verification_uri, verification_uri_complete, expires_at, action_hash, summary:[[label,value]], screening, failure_code, result:{ledger_ids, tx}}`. Errors use `{error, code, field}` with codes `INVALID_AGENT_ID, AGENT_NOT_FOUND, SCREENING_REFUSED, BANNED, CAP_EXCEEDED, MANDATE_INVALID, MANDATE_EXCEEDED, APPROVAL_CONSUMED, APPROVAL_EXPIRED`, plus (amendment) `PAYEE_MISMATCH` (403: the agent's ENS payout record differs from its profile) and `PAYEE_UNRESOLVED` (503). MCP authenticates with `Bearer MCP_API_TOKEN`; the engagement's human is whoever approves first (pairwise `sub`).

## 8. MCP tools — `agentslist_mcp/` (logic in `tools.py`, thin FastMCP `server.py`)

| Tool | Input | Behavior |
|---|---|---|
| `search_agents` | `{query, category?, max_budget_usdc?, limit?=10}` | list agents with AGT ids |
| `request_scope` | `{agent_id, outcome, budget_usdc, milestones?}` | validate AGT check digit locally first; create engagement; return SOW + hash |
| `submit_sow` (amendment) | `{path?, text?, agent_id?, budget_usdc?}` | read a local SOW file (or text), `POST /api/sow/parse`, return the scope; with `agent_id` (validated locally) also create the engagement with `source_document` and return engagement_id + sow_hash for `hire` |
| `hire` | `{engagement_id, agent_id, confirm_amount_usdc}` | re-validate id == engagement agent; start device approval; return user_code, verification_uri_complete, expires_at, action_hash, verdict |
| `release_milestone` | `{engagement_id, milestone_index}` | same, for release |
| `get_engagement_status` | `{engagement_id, wait_seconds?≤25}` | poll approval + engagement; block until terminal or timeout |
| `subhire` (stretch) | `{parent_engagement_id, agent_id, budget_usdc, category, mandate_token}` | sub-hire via mandate |

## 9. Agent IDs — `app/common/agent_ids.py`

`AGT-XXXX-XXXX-C`: 8 Crockford base32 chars (`0123456789ABCDEFGHJKMNPQRSTVWXYZ`) = 40-bit payload; check char = payload mod 37 over `0123456789ABCDEFGHJKMNPQRSTVWXYZ*~$=U`. Decode is case-insensitive, I/L→1, O→0, hyphens optional. `encode(n)`, `decode(s)` (raises `AgentIdError(code, suggestion)`), `is_valid(s)`, `from_db_id(id)` (deterministic keyed permutation). `agentslist_mcp/agent_ids.py` vendors a copy; a test asserts parity.

## 10. Names sidecar — `ens/` (Node, ENSjs + viem, 127.0.0.1:8787, header `X-Sidecar-Token`)

`POST /names/agent {agent_public_id, label, records}`, `POST /names/job {parent, label, expiry, records}`, `POST /names/subjob` (same shape), `POST /names/revoke {name}`, `GET /names/tree?root=`, `GET /health`. `DRY_RUN=1` returns deterministic fake tx hashes (tests). Record keys behind a constant map until the spike confirms ENSIP-25/26 names.

## 11. Data model — migration `0002_custody_chain`

- `humans`: id, world_sub (unique), created_at, last_seen_at, banned_at, ban_reason, weekly_cap_micro
- `agents` += public_id (unique), payout_address, screening_address, manifest_json, manifest_hash, manifest_stamped_at, manifest_stamp_approval_id, manifest_stamp_sub, ens_name
- `engagements`: id (ENG-…), agent_id, buyer_human_id, buyer_address, parent_engagement_id, depth, outcome, category, sow_json, sow_hash, total_micro, currency, status (`draft → scoped → awaiting_approval → funded → in_progress → completed | cancelled | refused`), deadline_at, mandate_id, ens_name, created_at, updated_at
- `milestones`: id, engagement_id, idx, title, acceptance, amount_micro, status (`pending → funded → submitted → released | held | refunded`), submitted_at, auto_release_at, released_ledger_id
- `ledger_entries`: id (LED-…), engagement_id, milestone_id, kind (`fund|release|refund|subhire_alloc|hold`), amount_micro, from_addr, to_addr, tx_hash, status (`simulated|pending|confirmed|failed`), approval_id, screening_id, created_at
- `approvals`: id (APR-…), kind, action_json, action_hash, nonce, flow, state, state_param, pkce_verifier, device_code, user_code, verification_uri, verification_uri_complete, poll_interval, next_poll_at, expires_at, human_sub, id_token_jti (unique), auth_time, acr, failure_code, failure_detail, consumed_at, engagement_id, milestone_id, agent_id, screening_id, created_at
- `approval_events`: id, approval_id, event, detail (JSON), at
- `mandates`: id (MND-…), parent_id, root_id, engagement_id, human_id, grantee_agent_public_id, budget_micro, spent_micro, categories, max_depth, depth, expires_at, token, approval_id, revoked_at
- `screenings`: id (SCR-…), hop, engagement_id, agent_id, chain_address, screened_address, network, verdict, cap_micro, reasons, toxic_score, traits, risk_group, raw, provider, fail_closed, latency_ms, created_at, expires_at
- `hire_intents`: id (HIT-…), immutable intent_hash, specialist and ENS name, endpoint path, server-owned task text and task_hash, Sepolia network/token/payee, integer amount, expiry, state (`created|payment_pending|delivered|expired|failed`), payer/mandate/ledger binding, stored deliverable and failure code
- `ens_names`: name (PK), node, kind (`root|agent|job|subjob`), parent_name, engagement_id, agent_id, owner, expiry, records, status (`pending|active|revoked|failed`), tx_hashes, updated_at
- `used_id_token_jtis`: jti (PK), seen_at

## 12. Environment variables

World: `WORLD_ISSUER=https://sandbox.auth.world.org`, `WORLD_CLIENT_ID`, `WORLD_CLIENT_SECRET`, `WORLD_REDIRECT_URI`, `WORLD_REQUIRED_ACR` · Approvals: `APPROVAL_TTL_SECONDS`, `STEPUP_MAX_AGE_SECONDS`, `HUMAN_WEEKLY_CAP_USDC` · Screening: `INTERCEPTA_API_KEY`, `INTERCEPTA_BASE_URL=https://api.web3antivirus.io`, `SCREENING_TIMEOUT_SECONDS`, `SCREENING_CAP_USDC`, `SCREENING_ADDRESS_MAP` · Escrow/mandates: `ESCROW_PRIVATE_KEY`, `BUYER_VAULT_PRIVATE_KEY`, `MANDATE_SIGNING_KEY`, `MANDATE_MAX_DEPTH=2` · Names: `ENS_SIDECAR_URL`, `ENS_SIDECAR_TOKEN`, `ENS_ROOT_NAME`, `ENS_OPERATOR_PRIVATE_KEY` (sidecar only) · MCP: `MCP_API_BASE`, `MCP_API_TOKEN`.

## Amendments

### 2026-09-26 — optional `spec_hash` in the operator manifest

The operator manifest (`build_manifest` in `app/seller/stamp.py`, whose hash a `manifest.publish` approval binds as `manifest_hash`) gains one optional key, so a stamp can also commit the operator to the agent's private runtime spec (prompts, limits, egress rules, human gates) without publishing it:

```json
{"v": 1, "agent_id": "AGT-…", "model": "…", "tools": [], "mcp_servers": [], "skills": [],
 "price_min_micro": 0, "price_max_micro": 0, "payout_address": "0x…",
 "spec_hash": "0x…"}
```

- `spec_hash` is `0x` + 64 lowercase hex characters. Input is trimmed and lowercased before the check; a blank value counts as absent, and any other value that does not match raises `ManifestError` on field `spec_hash`. The platform treats it as opaque; the runtime that produces it defines how. That computation must be deterministic and must cover the contents (or digests) of every file the spec references, such as prompt, playbook and rubric files, not just the file that names them; otherwise editing a prompt would leave `spec_hash` unchanged and never require a re-stamp.
- When absent, the key is omitted, not null. Every manifest and hash from before this amendment stays the same, and `v` stays 1.
- It is hashed like every other field. Adding, changing or removing it after a stamp means `RESTAMP_REQUIRED`. The manifest editor has an optional field for it, and the stamp diff lists it.
- The platform stores and stamps only the hash, never the spec. Checking that a running agent matches `spec_hash` is the runtime's job.
- The stamped value is read platform-side from the stamp's `manifest_snapshot` event (`stamped_manifest(agent)`). Public surfaces (`/api/agents`, listing cards) show at most `manifest_hash` and the stamp status, never the manifest itself, so an outside party can check a claimed `spec_hash` only by rebuilding the whole manifest with `build_manifest`'s normalization and comparing its hash with `manifest_hash`.
