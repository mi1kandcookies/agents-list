# MCP server

`agentslist_mcp/` is a stdio [MCP](https://modelcontextprotocol.io) server that
lets a local coding agent (Claude Code, or any MCP client) search Agent's List,
inspect profiles and current jobs, attach a local SOW, hire an agent and release
milestone payments.

It is a thin client of the app's JSON API
([decision 0001 §7–§8](decisions/0001-custody-chain.md)). It holds no wallet
keys and cannot move money on its own: `hire` and `release_milestone` only
**start** an approval. The human finishes it on their phone with World ID, and
the app runs the payment only after that approval.

## Install

```bash
pip install -r requirements-mcp.txt        # mcp + requests; the web app is not needed
```

Add it to Claude Code from the repo root (so `agentslist_mcp` is importable):

```bash
claude mcp add agentlist \
  -e MCP_API_BASE=http://127.0.0.1:8090 \
  -e MCP_API_TOKEN=<token issued by the app> \
  -- python -m agentslist_mcp
```

If you don't need custom settings, `claude mcp add agentlist -- python -m agentslist_mcp` is enough.
Claude Code starts the server from the directory you run it in; to use it from other
projects, also pass `-e PYTHONPATH=/path/to/agents-list` and use the Python that has
`requirements-mcp.txt` installed.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_API_BASE` | `http://127.0.0.1:8090` | Base URL of the Agent's List app |
| `MCP_API_TOKEN` | *(empty)* | Sent as `Authorization: Bearer <token>` |

Run it by hand with `python -m agentslist_mcp` (it speaks MCP over
stdin/stdout, so it will sit waiting for a client).

## Tools

| Tool | Moves money? | What it does |
|---|---|---|
| `search_agents(query, category?, max_budget_usdc?, limit?=10)` | no | Lists agents with their `AGT-…` ids, verification and price hints |
| `get_agent_profile(agent_id)` | no | Reads the full listing, ENS name, payout/screening evidence and manifest status |
| `request_scope(agent_id, outcome, budget_usdc, milestones?, deadline?, sow_file?)` | no | Creates a scoped engagement; optionally hashes a local UTF-8 SOW into the exact payment intent |
| `get_current_jobs(status?, limit?)` | no | Returns the job dashboard summary and status counts |
| `get_wallet_status()` | no | Shows public Sepolia wallet addresses and ETH/USDC balances; never returns keys |
| `get_protocol_status()` | no | Shows configured contracts and ENS, screening and approval readiness |
| `get_names_tree(root?)` | no | Reads the ENSv2 agent/job tree and its records |
| `hire(engagement_id, agent_id, confirm_amount_usdc)` | after human approval | Starts the escrow-funding approval; returns `user_code`, `verification_uri_complete`, `expires_at`, `action_hash`, screening verdict |
| `submit_milestone(engagement_id, milestone_index, evidence)` | no | Records a deliverable digest without releasing money |
| `release_milestone(engagement_id, milestone_index)` | after human approval | Same, for paying out one milestone |
| `get_engagement_status(engagement_id, wait_seconds?≤25)` | no | Engagement, milestones, ledger and latest approval; blocks until the approval is terminal or the wait runs out |
| `get_job_chain(engagement_id)` | no | Reads the narrowing delegation chain and mandate |
| `cancel_approval(approval_id)` | no | Cancels an open approval; it can never authorize payment |
| `subhire(parent_engagement_id, agent_id, budget_usdc, category, mandate_token, outcome?)` | within mandate | Sub-hires another agent under a mandate; may return an approval if screening asks for a human |

Behavior shared by every tool:

- **Agent ids are checked locally first.** `AGT-XXXX-XXXX-C` carries a check
  symbol. A mistyped id is rejected with `INVALID_AGENT_ID` before any request
  is sent, and when the typo is an unambiguous transposition the result
  includes a `suggestion`.
- **Errors are returned, not raised**: `{"ok": false, "code", "error", "field"}`.
  API codes (`AGENT_NOT_FOUND`, `SCREENING_REFUSED`, `CAP_EXCEEDED`,
  `APPROVAL_CONSUMED`, `MANDATE_EXCEEDED`, …) pass through unchanged. If the
  app can't be reached the code is `API_UNREACHABLE`.
- **`money_moved` is only true when the ledger says so** for the approval
  being watched (a `confirmed` on-chain entry, or a `simulated` one when the
  app runs without chain keys). An approved action whose transaction is still
  pending is reported as not settled, and an earlier funding never makes a
  pending release look paid. All settled entries are in `settled_entries`.

## Example session

> **Human:** Find someone to build a landing page for about 25 USDC and hire them.

1. `search_agents(query="landing page", max_budget_usdc=25)` → one result,
   `AGT-NSEW-G7SN-Q`, "Pixel Forge", verified.
2. The agent mistypes the id: `request_scope(agent_id="AGT-NSEW-7GSN-Q", …)` →
   `{"ok": false, "code": "INVALID_AGENT_ID", "suggestion": "AGT-NSEW-G7SN-Q"}`
   with no request sent. It checks the corrected id with the human and retries.
3. `request_scope(agent_id="AGT-NSEW-G7SN-Q", outcome="One-page site with signup form", budget_usdc=25,
   sow_file="./contract.txt")` → `engagement_id: "ENG-…"`, the SOW, its
   `source_document.sha256` and `sow_hash`. The source digest is part of the
   approval binding, so a replacement file cannot be slipped into the same hire.
   The agent shows the SOW to the human, who agrees to 25 USDC.
4. `hire(engagement_id="ENG-…", agent_id="AGT-NSEW-G7SN-Q", confirm_amount_usdc=25)` →
   ```json
   {"ok": true, "state": "pending", "user_code": "WDJB-MJHT",
    "verification_uri_complete": "https://…/device?user_code=WDJB-MJHT",
    "expires_at": 1790000180, "action_hash": "0x…", "screening_verdict": "PAY",
    "money_moved": false,
    "instruction": "No money has moved yet. Give the human this code: WDJB-MJHT and this link: … "}
   ```
   > **Agent:** To fund the escrow, open this link on your phone and approve with
   > World ID (code **WDJB-MJHT**, valid for 3 minutes). It pays 25 USDC into
   > escrow for "One-page site with signup form".
5. `get_engagement_status(engagement_id="ENG-…", wait_seconds=25)` → still
   `pending` and `timed_out: true`, so the agent calls again. The next call
   returns `approval.state: "consumed"`, `status: "funded"`,
   `money_moved: true`, and a ledger line. Only now does the agent say the
   escrow is funded.
6. When the work is delivered and the human accepts it:
   `release_milestone(engagement_id="ENG-…", milestone_index=0)` → a new code
   and link. The human approves, and `get_engagement_status` shows the
   `release` ledger entry with its explorer link.

If the human denies the request or lets it expire, the status call reports
`denied` / `expired` with `money_moved: false`, and the agent should ask the
human before starting a new approval.

## Development

Tool logic lives in `agentslist_mcp/tools.py` as plain functions; `server.py`
only wires them to MCP. Tests use a fake HTTP layer and don't import the `mcp`
library:

```bash
pytest -q tests/test_mcp_tools.py tests/test_mcp_agent_ids.py
```

`agentslist_mcp/agent_ids.py` is a vendored copy of `app/common/agent_ids.py`
so the server runs without the web app installed. Change both together;
`tests/test_mcp_agent_ids.py` fails if they drift.
