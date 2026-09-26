#!/usr/bin/env python3
"""
demo_check.py - walk a running Agent's List server through the hiring flow,
with a real human approving on their phone (not run in CI).

    python scripts/demo_check.py                          # https://127.0.0.1:8090
    python scripts/demo_check.py http://127.0.0.1:8090
    DEMO_CHECK_CA_BUNDLE="$(mkcert -CAROOT)/rootCA.pem" python scripts/demo_check.py

Steps: list agents -> create engagement -> hire (device flow) -> the human
approves on their phone -> state + ledger -> replay attempts -> release
milestone #1 (second approval) -> deny path -> expiry path (waits out the
approval TTL) -> sub-hire under the hired agent's mandate (refused without
one, refused over budget, then allowed; a human approves if screening asks)
-> pass/fail table.

Nothing is faked. Every approval is started on the server and finished (or
not) by the human with World ID; the script only reads the result. Whether
money moves on Sepolia depends on the server: without escrow keys it records
simulated ledger entries. The script itself holds no keys and signs nothing.

Environment:
    MCP_API_TOKEN          bearer token, if the server requires one; the server
                           returns the hired agent's mandate token only to it
    DEMO_MANDATE_TOKEN     mandate token to sub-hire with, if you have one
    DEMO_CHECK_CA_BUNDLE   CA file for a local HTTPS certificate (mkcert root CA)
    DEMO_CHECK_INSECURE=1  skip TLS verification (local testing only)
    REQUESTS_CA_BUNDLE     also honored, as in any requests-based tool
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import requests

DEFAULT_BASE = "https://127.0.0.1:8090"
TERMINAL = {"consumed", "denied", "expired", "cancelled", "rejected", "blocked", "failed"}


# ── output helpers ──────────────────────────────────────────────────────────

def say(text: str = "") -> None:
    print(text, flush=True)


def heading(text: str) -> None:
    say()
    say(f"== {text} " + "=" * max(0, 60 - len(text)))


def usdc(micro) -> str:
    return f"{(micro or 0) / 1_000_000:,.2f} USDC"


def table(rows: list[list[str]], headers: list[str]) -> str:
    cells = [headers] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    line = "  ".join("-" * w for w in widths)
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), line]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in cells[1:]]
    return "\n".join(out)


@dataclass
class Results:
    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, step: str, outcome: str, detail: str = "") -> None:
        self.rows.append((step, outcome, detail))
        say(f"  -> {outcome}: {step}" + (f" ({detail})" if detail else ""))

    def check(self, step: str, ok: bool, detail: str = "") -> bool:
        self.add(step, "PASS" if ok else "FAIL", detail)
        return ok

    @property
    def failed(self) -> bool:
        return any(r[1] == "FAIL" for r in self.rows)


class ApiError(Exception):
    pass


# ── API client ──────────────────────────────────────────────────────────────

class Api:
    def __init__(self, base: str, *, session: requests.Session | None = None, timeout: float = 15):
        self.base = base.rstrip("/")
        self.http = session or requests.Session()
        self.timeout = timeout
        token = os.environ.get("MCP_API_TOKEN")
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"
        if os.environ.get("DEMO_CHECK_INSECURE") == "1":
            self.http.verify = False
            import urllib3
            urllib3.disable_warnings()
        elif os.environ.get("DEMO_CHECK_CA_BUNDLE"):
            self.http.verify = os.environ["DEMO_CHECK_CA_BUNDLE"]

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        try:
            resp = self.http.request(method, self.base + path, json=body, timeout=self.timeout)
        except requests.exceptions.SSLError as exc:
            raise ApiError(f"TLS verification failed for {self.base}: set DEMO_CHECK_CA_BUNDLE "
                           f"to your local CA ({exc.__class__.__name__})") from None
        except requests.RequestException as exc:
            raise ApiError(f"cannot reach {self.base}: {exc.__class__.__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            data = {"error": resp.text[:200]}
        return resp.status_code, data if isinstance(data, dict) else {"items": data}

    def url(self, path: str) -> str:
        return self.base + path


# ── flow helpers ────────────────────────────────────────────────────────────

def create_engagement(api: Api, agent_id: str, outcome: str, budget: str) -> tuple[int, dict]:
    total = Decimal(budget)
    half = (total / 2).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    half, rest = str(half), str(total - half)
    return api.call("POST", "/api/engagements", {
        "agent_id": agent_id, "outcome": outcome, "budget_usdc": budget,
        "milestones": [
            {"title": "First draft", "acceptance": "Draft delivered for review", "amount_usdc": half},
            {"title": "Final delivery", "acceptance": "Revisions applied and accepted", "amount_usdc": rest},
        ],
    })


def show_approval_prompt(api: Api, approval: dict, instruction: str) -> None:
    def row(label, value):
        say(f"  {label:<18} {value}")

    for label, value in approval.get("summary") or []:
        row(label, value)
    screening = approval.get("screening") or {}
    if screening:
        row("Screening verdict", screening.get("verdict"))
    row("User code", approval.get("user_code"))
    row("Link", approval.get("verification_uri_complete") or approval.get("verification_uri"))
    row("Approval page", api.url(approval.get("url") or f"/approvals/{approval.get('approval_id')}"))
    expires = approval.get("expires_at")
    if expires:
        row("Expires in", f"{max(0, int(expires - time.time()))} s")
    say()
    say(f"  >>> {instruction}")


def wait_terminal(api: Api, approval_id: str, *, limit_s: float | None = None) -> dict:
    """Poll GET /api/approvals/<id> (which advances the device flow on the
    server) until the approval reaches a terminal state."""
    last, started = None, time.time()
    while True:
        status, data = api.call("GET", f"/api/approvals/{approval_id}")
        if status != 200:
            raise ApiError(f"approval poll returned {status}: {data.get('code')}")
        state = data.get("state")
        if state != last:
            say(f"  state: {state}")
            last = state
        if state in TERMINAL:
            return data
        if limit_s is not None and time.time() - started > limit_s:
            return data
        time.sleep(3)


def ask(prompt: str) -> str:
    try:
        return input(f"  {prompt} ").strip().lower()
    except EOFError:
        return ""


def fund_entries(eng: dict) -> list[dict]:
    return [e for e in eng.get("ledger") or [] if e.get("kind") == "fund"
            and e.get("status") != "failed"]


def print_engagement(eng: dict) -> None:
    say(f"  engagement {eng.get('engagement_id')}  status={eng.get('status')}  "
        f"escrow={eng.get('escrow_mode')}  total={usdc(eng.get('total_micro'))}")
    say(table([[m["idx"] + 1, m["title"], usdc(m["amount_micro"]), m["status"]]
               for m in eng.get("milestones") or []], ["#", "milestone", "amount", "status"]))
    ledger = eng.get("ledger") or []
    if ledger:
        say(table([[e["id"], e["kind"], usdc(e["amount_micro"]), e["status"],
                    e.get("explorer") or ("simulated" if e.get("simulated") else "-")]
                   for e in ledger], ["entry", "kind", "amount", "status", "tx"]))
    else:
        say("  ledger: empty (no money has moved)")


def hire_device(api: Api, eng: dict) -> tuple[int, dict]:
    total = f"{eng['total_micro'] / 1_000_000:.2f}"
    return api.call("POST", f"/api/engagements/{eng['engagement_id']}/hire",
                    {"flow": "device", "confirm_amount_usdc": total})


# ── steps ───────────────────────────────────────────────────────────────────

def step_list(api: Api, res: Results, wanted: str | None) -> tuple[dict | None, list[dict]]:
    heading("1. List agents")
    status, data = api.call("GET", "/api/agents?per_page=50")
    agents = data.get("agents") or []
    if not res.check("list agents", status == 200 and bool(agents), f"{len(agents)} listed"):
        say("  No agents. Run `flask --app wsgi seed-demo` on the server first.")
        return None, agents
    say(table([[a.get("agent_id"), a["name"][:34], a["category"],
                f"${a['min_price']:.2f}-${a['max_price']:.2f}/min"
                if a.get("billing") == "per_minute" else a.get("billing"),
                "yes" if a.get("operator_stamped") else "no"]
               for a in agents[:12]], ["agent id", "name", "category", "price", "stamped"]))
    if wanted:
        agent = next((a for a in agents if a.get("agent_id") == wanted.upper()), None)
        if agent is None:
            res.add("pick agent", "FAIL", f"{wanted} not listed")
        return agent, agents
    stamped = [a for a in agents if a.get("operator_stamped")]
    return (stamped or agents)[0], agents


def step_create(api: Api, res: Results, agent: dict, budget: str, label: str) -> dict | None:
    status, data = create_engagement(
        api, agent["agent_id"], f"Demo check ({label}): one-page summary of our Q3 pipeline", budget)
    ok = status == 201
    res.check(f"create engagement ({label})", ok,
              data.get("engagement_id") if ok else f"{status} {data.get('code')}")
    if ok:
        preview = data.get("screening") or {}
        say(f"  sow_hash {data.get('sow_hash')}  screening preview: "
            f"{preview.get('verdict', 'none')}{' (fail closed)' if preview.get('fail_closed') else ''}")
        return data
    return None


def step_hire_and_approve(api: Api, res: Results, eng: dict) -> dict | None:
    heading("3. Hire (device flow)")
    status, approval = hire_device(api, eng)
    if status != 202:
        code = approval.get("code")
        res.check("start hire approval", False, f"{status} {code}")
        if code in ("NOT_STAMPED", "RESTAMP_REQUIRED"):
            say("  The agent's operator has not stamped its current manifest. Stamp it from "
                "/seller/agents/<id>/manifest with World ID, or in development run "
                "`flask --app wsgi seed-demo --dev-stamp`.")
        if code == "SCREENING_REFUSED":
            reasons = ((approval.get("screening") or {}).get("reasons") or [{}])
            say(f"  Screening refused the payee ({reasons[0].get('code')}). A payee with no "
                "mapped mainnet screening address is always refused (fail closed).")
        return None
    res.check("start hire approval", approval.get("state") == "pending",
              f"{approval.get('approval_id')} state={approval.get('state')}")
    heading("4. Human approval")
    show_approval_prompt(api, approval, "Open the link on your phone and APPROVE with World ID.")
    final = wait_terminal(api, approval["approval_id"])
    res.check("human approved, funding executed", final.get("state") == "consumed",
              f"state={final.get('state')} {final.get('failure_code') or ''}".strip())
    return final


def step_state(api: Api, res: Results, eng_id: str) -> dict:
    heading("5. Engagement state and ledger")
    _, eng = api.call("GET", f"/api/engagements/{eng_id}")
    print_engagement(eng)
    funds = fund_entries(eng)
    res.check("engagement funded with one fund entry",
              eng.get("status") == "funded" and len(funds) == 1,
              f"status={eng.get('status')} fund entries={len(funds)}")
    return eng


def step_replay(api: Api, res: Results, eng: dict, approval: dict) -> None:
    heading("6. Replay attempts")
    status, data = hire_device(api, eng)
    res.check("second hire on a funded engagement is refused", status == 409,
              f"{status} {data.get('code')}")
    status, data = api.call("POST", f"/api/approvals/{approval['approval_id']}/cancel", {})
    res.check("consumed approval cannot be cancelled or reused", status == 409,
              f"{status} {data.get('code')}")
    _, again = api.call("GET", f"/api/engagements/{eng['engagement_id']}")
    res.check("still exactly one fund entry", len(fund_entries(again)) == 1,
              f"{len(fund_entries(again))} fund entries")


def step_release(api: Api, res: Results, eng_id: str) -> None:
    heading("7. Release milestone #1")
    status, approval = api.call("POST", f"/api/engagements/{eng_id}/milestones/0/release",
                                {"flow": "device"})
    if not res.check("start release approval", status == 202,
                     f"{status} {approval.get('code') or approval.get('approval_id')}"):
        return
    show_approval_prompt(api, approval, "APPROVE this release on your phone.")
    final = wait_terminal(api, approval["approval_id"])
    res.check("release approved and executed", final.get("state") == "consumed",
              f"state={final.get('state')}")
    _, eng = api.call("GET", f"/api/engagements/{eng_id}")
    print_engagement(eng)
    releases = [e for e in eng.get("ledger") or [] if e.get("kind") == "release"]
    m0 = (eng.get("milestones") or [{}])[0]
    res.check("milestone #1 released in the ledger", bool(releases) and m0.get("status") in
              ("released", "held"), f"milestone status={m0.get('status')}")


def step_deny(api: Api, res: Results, agent: dict, budget: str) -> None:
    heading("8. Deny path")
    if ask("Run the deny path? You will DENY the request on your phone. [Y/n]") == "n":
        res.add("deny path", "SKIP", "skipped by operator")
        return
    eng = step_create(api, res, agent, budget, "deny")
    if not eng:
        return
    status, approval = hire_device(api, eng)
    if not res.check("start approval to deny", status == 202,
                     f"{status} {approval.get('code') or approval.get('approval_id')}"):
        return
    show_approval_prompt(api, approval, "Open the link and DENY the request.")
    final = wait_terminal(api, approval["approval_id"])
    _, after = api.call("GET", f"/api/engagements/{eng['engagement_id']}")
    res.check("denied, no money moved", final.get("state") == "denied" and not after.get("ledger"),
              f"state={final.get('state')} ledger entries={len(after.get('ledger') or [])}")


def step_expire(api: Api, res: Results, agent: dict, budget: str) -> None:
    heading("9. Expiry path")
    if ask("Run the expiry path? Do NOT open the link; the script waits out the TTL "
           "(3 minutes by default). [Y/n]") == "n":
        res.add("expiry path", "SKIP", "skipped by operator")
        return
    eng = step_create(api, res, agent, budget, "expire")
    if not eng:
        return
    status, approval = hire_device(api, eng)
    if not res.check("start approval to let expire", status == 202,
                     f"{status} {approval.get('code') or approval.get('approval_id')}"):
        return
    wait = max(0, int((approval.get("expires_at") or time.time()) - time.time()))
    say(f"  Leave it alone. Code {approval.get('user_code')} expires in {wait} s; waiting...")
    final = wait_terminal(api, approval["approval_id"], limit_s=wait + 60)
    _, after = api.call("GET", f"/api/engagements/{eng['engagement_id']}")
    res.check("expired, no money moved", final.get("state") == "expired" and not after.get("ledger"),
              f"state={final.get('state')}")


def _as_mandate(api: Api, token: str | None, method: str, path: str, body: dict):
    """One request with ``Authorization: Mandate <token>`` (or none)."""
    http = api.http
    saved = http.headers.pop("Authorization", None)
    if token:
        http.headers["Authorization"] = f"Mandate {token}"
    try:
        return api.call(method, path, body)
    finally:
        http.headers.pop("Authorization", None)
        if saved:
            http.headers["Authorization"] = saved


def step_subhire(api: Api, res: Results, eng: dict, funded: bool, agents: list[dict]) -> None:
    heading("10. Sub-hire")
    path = f"/api/engagements/{eng['engagement_id']}/subhire"
    status, data = _as_mandate(api, None, "POST", path, {})
    if status in (404, 405) and data.get("code") in (None, "NOT_FOUND"):
        res.add("sub-hire", "SKIP", "endpoint not present on this server")
        return
    res.check("sub-hire without a mandate is refused", status == 401,
              f"{status} {data.get('code')}")
    if not funded:
        res.add("sub-hire with mandate", "SKIP", "hire was not approved")
        return
    _, parent = api.call("GET", f"/api/engagements/{eng['engagement_id']}")
    token = os.environ.get("DEMO_MANDATE_TOKEN") or parent.get("mandate_token")
    if not token:
        res.add("sub-hire with mandate", "SKIP",
                "no mandate token: set MCP_API_TOKEN (the server returns it only to token "
                "holders) or DEMO_MANDATE_TOKEN")
        return
    child_agent = next((a for a in agents if a.get("agent_id") != eng["agent_id"]
                        and a.get("operator_stamped", True)), None)
    if child_agent is None:
        res.add("sub-hire with mandate", "SKIP", "no second stamped agent listed")
        return
    body = {"agent_id": child_agent["agent_id"], "outcome": "Demo check: sub-task",
            "category": eng.get("category")}
    status, data = _as_mandate(api, token, "POST", path, {**body, "budget_usdc": "1000000"})
    res.check("sub-hire over the mandate budget is refused",
              status == 403 and data.get("code") == "MANDATE_EXCEEDED",
              f"{status} {data.get('code')}")
    say(f"  sub-hiring {child_agent['agent_id']} {child_agent['name']} for 0.50 USDC "
        f"(category {body['category']})")
    status, data = _as_mandate(api, token, "POST", path, {**body, "budget_usdc": "0.50"})
    if status == 202:
        show_approval_prompt(api, data, "Screening asked for a human: APPROVE this sub-hire "
                                        "on your phone.")
        final = wait_terminal(api, data["approval_id"])
        res.check("sub-hire approved by the root human", final.get("state") == "consumed",
                  f"state={final.get('state')}")
    else:
        res.check("sub-hire within the mandate", status == 201,
                  f"{status} {data.get('code') or data.get('engagement_id')}")
    _, chain = api.call("GET", f"/api/engagements/{eng['engagement_id']}/chain")
    say(f"  custody chain now: {len(chain.get('nodes') or [])} nodes, "
        f"{len(chain.get('edges') or [])} edges")


# ── main ────────────────────────────────────────────────────────────────────

def run(api: Api, args) -> Results:
    res = Results()
    agent, agents = step_list(api, res, args.agent)
    if agent is None:
        return res
    heading("2. Create engagement")
    say(f"  agent {agent['agent_id']}  {agent['name']}")
    eng = step_create(api, res, agent, args.budget, "hire")
    if eng is None:
        return res
    approval = step_hire_and_approve(api, res, eng)
    funded = bool(approval and approval.get("state") == "consumed")
    if funded:
        state = step_state(api, res, eng["engagement_id"])
        step_replay(api, res, state, approval)
        step_release(api, res, eng["engagement_id"])
        _, chain = api.call("GET", f"/api/engagements/{eng['engagement_id']}/chain")
        say(f"  custody chain: {len(chain.get('nodes') or [])} nodes, "
            f"{len(chain.get('edges') or [])} edges")
    else:
        for step in ("state and ledger", "replay", "release milestone #1"):
            res.add(step, "SKIP", "hire was not approved")
    step_deny(api, res, agent, args.budget)
    if args.skip_expire:
        res.add("expiry path", "SKIP", "--skip-expire")
    else:
        step_expire(api, res, agent, args.budget)
    step_subhire(api, res, eng, funded, agents)
    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Human-in-the-loop end-to-end check.")
    parser.add_argument("base_url", nargs="?", default=DEFAULT_BASE,
                        help=f"server base URL (default {DEFAULT_BASE})")
    parser.add_argument("--agent", help="AGT-… id to hire (default: first listed)")
    parser.add_argument("--budget", default="2.00", help="engagement budget in USDC (default 2.00)")
    parser.add_argument("--skip-expire", action="store_true", help="skip the TTL wait")
    args = parser.parse_args(argv)
    try:
        if Decimal(args.budget) < Decimal("0.02"):
            raise InvalidOperation
    except InvalidOperation:
        parser.error("--budget must be a USDC amount of at least 0.02 (two milestones)")

    api = Api(args.base_url)
    say(f"Agent's List demo check against {api.base}")
    try:
        res = run(api, args)
    except ApiError as exc:
        say(f"\nERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        say("\nInterrupted.")
        return 130
    heading("Summary")
    say(table([list(r) for r in res.rows], ["step", "result", "detail"]))
    passed = sum(1 for r in res.rows if r[1] == "PASS")
    say(f"\n{passed} passed, {sum(1 for r in res.rows if r[1] == 'FAIL')} failed, "
        f"{sum(1 for r in res.rows if r[1] == 'SKIP')} skipped")
    return 1 if res.failed or not res.rows else 0


if __name__ == "__main__":
    sys.exit(main())
