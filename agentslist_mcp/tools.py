"""MCP tool logic (docs/decisions/0001-custody-chain.md §8), free of the ``mcp``
library so it can be tested with a fake HTTP layer.

Every tool returns a JSON-able dict: ``{"ok": True, ...}`` on success, or
``{"ok": False, "code", "error", "field", ...}`` on failure, where API error
codes are passed through verbatim.

Money never moves inside a tool call. ``hire``, ``release_milestone`` and
(sometimes) ``subhire`` only start an approval; the human finishes it on their
phone, and only ``get_engagement_status`` reports whether funds moved, based
on the ledger.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from agentslist_mcp import agent_ids
from agentslist_mcp.client import AgentListAPIError

MAX_WAIT_SECONDS = 25
POLL_INTERVAL_SECONDS = 2.0
MAX_SEARCH_LIMIT = 50
MAX_SOW_FILE_BYTES = 128 * 1024
# Full SOW parsing mirrors the web upload path; raw scope is never sent to the
# payment endpoint until the human reviews the parsed result.
SOW_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
SOW_MAX_BYTES = 5 * 1024 * 1024

# §3 approval states. "approved" is not terminal: the executor runs next and
# the approval becomes "consumed" (success) or "failed".
TERMINAL_APPROVAL_STATES = frozenset({"consumed", "denied", "expired", "cancelled", "rejected", "blocked", "failed"})
# §11 engagement statuses with nothing left to wait for.
TERMINAL_ENGAGEMENT_STATUSES = frozenset({"completed", "cancelled", "refused"})
# §11 ledger kinds that move funds, and statuses that mean it happened.
_MONEY_KINDS = frozenset({"fund", "release", "refund", "subhire_alloc"})
_SETTLED = frozenset({"confirmed", "simulated"})


# --- helpers ----------------------------------------------------------------

def _error(code: str, error: str, field: str | None = None, **extra) -> dict:
    return {"ok": False, "code": code, "error": error, "field": field, **extra}


def _api_error(exc: AgentListAPIError) -> dict:
    return _error(exc.code, exc.error, exc.field, http_status=exc.status)


def _check_agent_id(value, field: str = "agent_id") -> tuple[str | None, dict | None]:
    """Normalize an AGT id locally. Returns (id, None) or (None, error)."""
    try:
        return agent_ids.normalize(value), None
    except agent_ids.AgentIdError as exc:
        hint = (f"Did you mean {exc.suggestion}? Confirm with the human before retrying."
                if exc.suggestion else
                "Re-check the id with search_agents; ids look like AGT-XXXX-XXXX-C.")
        return None, _error("INVALID_AGENT_ID", f"{value!r} is not a valid agent id ({exc.code}). {hint}",
                            field, reason=exc.code, suggestion=exc.suggestion)


def _check_usdc(value, field: str) -> tuple[float | int | None, dict | None]:
    """Positive USDC amount with at most 6 decimals (micro-USDC)."""
    if isinstance(value, bool):
        return None, _error("INVALID_AMOUNT", f"{field} must be a number of USDC", field)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None, _error("INVALID_AMOUNT", f"{field} must be a number of USDC", field)
    if not d.is_finite() or d <= 0:
        return None, _error("INVALID_AMOUNT", f"{field} must be greater than 0", field)
    if d != d.quantize(Decimal("0.000001")):
        return None, _error("INVALID_AMOUNT", f"{field} has more than 6 decimal places", field)
    return (int(d) if d == d.to_integral_value() else float(d)), None


def _require_text(value, field: str) -> dict | None:
    if not isinstance(value, str) or not value.strip():
        return _error("INVALID_REQUEST", f"{field} is required", field)
    return None


def _sow_document(path: str | None) -> tuple[dict | None, dict | None]:
    """Read a caller-selected local text SOW before creating the hash-bound job."""
    if path in (None, ""):
        return None, None
    if not isinstance(path, str) or not path.strip():
        return None, _error("INVALID_REQUEST", "sow_file must be a path", "sow_file")
    try:
        file_path = Path(path).expanduser()
        raw = file_path.read_bytes()
    except (OSError, ValueError) as exc:
        return None, _error("SOW_FILE_UNREADABLE", f"could not read sow_file: {exc.__class__.__name__}",
                            "sow_file")
    if len(raw) > MAX_SOW_FILE_BYTES:
        return None, _error("SOW_DOCUMENT_TOO_LARGE", f"sow_file is larger than {MAX_SOW_FILE_BYTES} bytes",
                            "sow_file")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, _error("SOW_DOCUMENT_INVALID", "sow_file must be UTF-8 text", "sow_file")
    return {"filename": file_path.name, "content": content}, None


def _engagement(body: dict) -> dict:
    """GET /api/engagements/<id> may nest the engagement or return it flat."""
    inner = body.get("engagement") if isinstance(body, dict) else None
    return inner if isinstance(inner, dict) else (body or {})


def _engagement_id(eng: dict):
    return eng.get("engagement_id") or eng.get("id")


def _screening(approval: dict) -> dict | None:
    s = approval.get("screening")
    if not isinstance(s, dict):
        return None
    return {k: s.get(k) for k in ("id", "verdict", "cap_micro", "reasons", "fail_closed")}


def _approval_view(approval: dict, *, engagement_id) -> dict:
    """Shape an approval for the calling agent, with a plain instruction."""
    state = approval.get("state")
    screening = _screening(approval)
    verdict = (screening or {}).get("verdict")
    view = {
        "ok": True,
        "engagement_id": engagement_id,
        "approval_id": approval.get("approval_id"),
        "kind": approval.get("kind"),
        "state": state,
        "user_code": approval.get("user_code"),
        "verification_uri": approval.get("verification_uri"),
        "verification_uri_complete": approval.get("verification_uri_complete"),
        "expires_at": approval.get("expires_at"),
        "action_hash": approval.get("action_hash"),
        "summary": approval.get("summary"),
        "screening": screening,
        "screening_verdict": verdict,
        "failure_code": approval.get("failure_code"),
        "money_moved": False,
    }
    if state in ("approved", "consumed"):
        view["instruction"] = ("The human approved this action and the app is executing it. Money has moved "
                               "only once get_engagement_status reports money_moved=true for this approval.")
        return view
    if state in TERMINAL_APPROVAL_STATES:
        why = approval.get("failure_code") or (f"screening verdict {verdict}" if state == "blocked" else state)
        view["instruction"] = (f"This approval ended as '{state}' ({why}). No money moved for it. "
                               "Tell the human; do not retry automatically.")
        return view
    link = approval.get("verification_uri_complete") or approval.get("verification_uri")
    lines = [
        f"No money has moved yet. Give the human this code: {approval.get('user_code')} "
        f"and this link: {link} . They open it on their phone and approve with World ID "
        f"before it expires (expires_at={approval.get('expires_at')}).",
        f"Tell them the approval covers action_hash {approval.get('action_hash')} and read them the summary.",
    ]
    if verdict in ("ASK_HUMAN", "CAP"):
        lines.append(f"Screening returned {verdict}: show the human the screening reasons before they approve.")
    lines.append(f"Then call get_engagement_status(engagement_id={engagement_id!r}, wait_seconds={MAX_WAIT_SECONDS}) "
                 "until it reports a terminal state. Do not say the payment happened until it does.")
    view["instruction"] = " ".join(lines)
    return view


def _settled(ledger) -> list[dict]:
    return [e for e in (ledger or []) if isinstance(e, dict)
            and e.get("kind") in _MONEY_KINDS and e.get("status") in _SETTLED]


def _for_approval(entries, approval: dict | None) -> list[dict]:
    """Ledger entries produced by ``approval`` (by approval_id or result.ledger_ids).
    Without an approval, every entry counts."""
    if approval is None:
        return list(entries)
    ids = set(((approval.get("result") or {}).get("ledger_ids")) or [])
    aid = approval.get("approval_id")
    return [e for e in entries if (aid and e.get("approval_id") == aid) or e.get("id") in ids]


def _price(agent: dict):
    try:
        d = Decimal(str(agent.get("price_hint_usdc")))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _pick_approval(body: dict, eng: dict) -> dict | None:
    """The approval to watch: the newest non-terminal one, else the newest."""
    approvals = body.get("approvals") or eng.get("approvals") or []
    approvals = [a for a in approvals if isinstance(a, dict) and a.get("approval_id")]
    if not approvals:
        return None
    open_ = [a for a in approvals if a.get("state") not in TERMINAL_APPROVAL_STATES]
    return (open_ or approvals)[-1]


# --- tools ------------------------------------------------------------------

def search_agents(client, query: str = "", category: str | None = None,
                  max_budget_usdc=None, limit: int = 10) -> dict:
    try:
        limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    except (TypeError, ValueError):
        limit = 10
    if max_budget_usdc is not None:
        max_budget_usdc, err = _check_usdc(max_budget_usdc, "max_budget_usdc")
        if err:
            return err
    try:
        body = client.search_agents(query or "", category=category, limit=limit)
    except AgentListAPIError as exc:
        return _api_error(exc)
    agents = body.get("agents", []) if isinstance(body, dict) else list(body or [])
    if max_budget_usdc is not None:
        # Keep agents without a usable price hint; the SOW sets the real price.
        cap = Decimal(str(max_budget_usdc))
        agents = [a for a in agents if (_price(a) is None or _price(a) <= cap)]
    agents = agents[:limit]
    return {"ok": True, "count": len(agents), "agents": agents,
            "next_step": "Pick one with the human, then call request_scope with its agent_id (AGT-...)."}


def get_agent_profile(client, agent_id: str) -> dict:
    agent_id, err = _check_agent_id(agent_id)
    if err:
        return err
    try:
        profile = client.get_agent_profile(agent_id)
    except AgentListAPIError as exc:
        return _api_error(exc)
    return {"ok": True, "agent": profile,
            "next_step": "Use this agent_id with request_scope after the human confirms the brief and budget."}


def request_scope(client, agent_id: str, outcome: str, budget_usdc, milestones: list | None = None,
                  deadline: str | int | None = None, sow_file: str | None = None) -> dict:
    agent_id, err = _check_agent_id(agent_id)
    if err:
        return err
    err = _require_text(outcome, "outcome")
    if err:
        return err
    budget_usdc, err = _check_usdc(budget_usdc, "budget_usdc")
    if err:
        return err
    payload = {"agent_id": agent_id, "outcome": outcome.strip(), "budget_usdc": budget_usdc}
    if milestones:
        if not isinstance(milestones, list):
            return _error("INVALID_REQUEST", "milestones must be a list", "milestones")
        clean = []
        for i, m in enumerate(milestones):
            if not isinstance(m, dict) or _require_text(m.get("title"), "title"):
                return _error("INVALID_REQUEST", f"milestones[{i}] needs a title", f"milestones[{i}].title")
            amount, err = _check_usdc(m.get("amount_usdc"), f"milestones[{i}].amount_usdc")
            if err:
                return err
            clean.append({"title": m["title"].strip(), "acceptance": (m.get("acceptance") or "").strip(),
                          "amount_usdc": amount})
        payload["milestones"] = clean
    if deadline not in (None, ""):
        payload["deadline"] = deadline
    document, err = _sow_document(sow_file)
    if err:
        return err
    if document is not None:
        payload["sow_document"] = document
    try:
        eng = _engagement(client.create_engagement(payload))
    except AgentListAPIError as exc:
        return _api_error(exc)
    eid = _engagement_id(eng)
    return {
        "ok": True,
        "engagement_id": eid,
        "agent_id": eng.get("agent_id", agent_id),
        "status": eng.get("status"),
        "sow": eng.get("sow") or eng.get("sow_json"),
        "sow_hash": eng.get("sow_hash"),
        "milestones": eng.get("milestones"),
        "screening": eng.get("screening"),
        "money_moved": False,
        "next_step": ("Nothing is paid yet. Show the human the SOW, milestones and total. If they agree, "
                      f"call hire(engagement_id={eid!r}, agent_id={agent_id!r}, confirm_amount_usdc=<total>)."),
    }


def get_current_jobs(client, status: str | None = None, limit: int = 20) -> dict:
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    if status is not None and (not isinstance(status, str) or not status.strip()):
        return _error("INVALID_REQUEST", "status must be a non-empty string", "status")
    try:
        body = client.list_engagements(status=status, limit=limit)
    except AgentListAPIError as exc:
        return _api_error(exc)
    jobs = body.get("engagements", []) if isinstance(body, dict) else []
    counts = {}
    for job in jobs:
        state = job.get("status", "unknown") if isinstance(job, dict) else "unknown"
        counts[state] = counts.get(state, 0) + 1
    return {"ok": True, "count": len(jobs), "jobs": jobs, "by_status": counts,
            "next_step": "Use get_engagement_status for one job, or get_agent_profile before starting a new SOW."}


def get_wallet_status(client) -> dict:
    try:
        body = client.get_wallet_status()
    except AgentListAPIError as exc:
        return _api_error(exc)
    return {"ok": True, **body,
            "note": "Private keys remain on the app host; this view contains public addresses and balances only."}


def get_protocol_status(client) -> dict:
    try:
        body = client.get_protocol_status()
    except AgentListAPIError as exc:
        return _api_error(exc)
    return {"ok": True, **body}


def get_names_tree(client, root: str | None = None) -> dict:
    if root is not None and (not isinstance(root, str) or not root.strip()):
        return _error("INVALID_REQUEST", "root must be a non-empty ENS name", "root")
    try:
        body = client.get_names_tree(root=root)
    except AgentListAPIError as exc:
        return _api_error(exc)
    return {"ok": True, **body,
            "note": "ENS records are displayable here; payment authorization still re-resolves critical payee records."}


# --- statement of work upload -----------------------------------------------

_SCOPE_KEYS = ("method", "title", "outcome", "brief", "category_key", "category_label", "agent_category",
               "milestones", "acceptance", "deadline", "budget_usdc", "warnings", "notes")


def _read_sow(path: str) -> tuple[str | None, bytes | None, dict | None]:
    """Read an allowed local SOW without sending a path to the server."""
    full = os.path.abspath(os.path.expanduser(path.strip()))
    name = os.path.basename(full)
    if not os.path.isfile(full):
        return None, None, _error("FILE_NOT_FOUND", f"no file at {full}", "path")
    if os.path.splitext(name)[1].lower() not in SOW_EXTENSIONS:
        return None, None, _error("UNSUPPORTED_FILE_TYPE", "the SOW must be a .pdf, .docx, .txt or .md file", "path")
    size = os.path.getsize(full)
    if size > SOW_MAX_BYTES:
        return None, None, _error("FILE_TOO_LARGE", f"{name} is {size} bytes; the limit is 5 MB", "path")
    if size == 0:
        return None, None, _error("EMPTY_DOCUMENT", f"{name} is empty", "path")
    try:
        with open(full, "rb") as fh:
            return name, fh.read(), None
    except OSError as exc:
        return None, None, _error("FILE_UNREADABLE", f"could not read {full}: {exc.strerror or exc}", "path")


def _sow_milestones(scope: dict, budget: Decimal) -> tuple[list | None, str | None]:
    """Convert parsed milestones only when their acceptance and totals are safe."""
    ms = [m for m in (scope.get("milestones") or []) if isinstance(m, dict) and m.get("title")]
    if not ms:
        return None, "The document has no milestones, so the SOW uses one milestone for the whole outcome."
    if any(not m.get("acceptance") for m in ms):
        return None, ("Some milestones in the document have no acceptance criteria, so the SOW uses one "
                      "milestone for the whole outcome. To keep the document's milestones, ask the human for "
                      "the missing criteria and call request_scope with milestones.")
    amounts = [m.get("amount_usdc") for m in ms]
    micro = int(budget * 1_000_000)
    if all(a is None for a in amounts):
        each = micro // len(ms)
        parts = [each] * (len(ms) - 1) + [micro - each * (len(ms) - 1)]
        note = "The document has no milestone amounts, so the budget was split evenly across its milestones."
    elif any(a is None for a in amounts):
        return None, ("Some milestones in the document have no amount, so the SOW uses one milestone for the "
                      "whole outcome. Ask the human for the amounts and call request_scope with milestones.")
    else:
        try:
            parts = [int(Decimal(str(a)) * 1_000_000) for a in amounts]
        except (InvalidOperation, ValueError):
            return None, "A milestone amount was not a valid USDC number, so the SOW uses one milestone."
        if sum(parts) != micro:
            return None, (f"The milestone amounts add up to {Decimal(sum(parts)) / 1_000_000} USDC, not the "
                          f"budget of {budget} USDC, so the SOW uses one milestone for the whole outcome.")
        note = None
    out = [{"title": str(m["title"]).strip(),
            "acceptance": "\n".join("- " + str(c).strip() for c in m["acceptance"] if str(c).strip()),
            "amount_usdc": format(Decimal(p).scaleb(-6).normalize(), "f")} for m, p in zip(ms, parts)]
    return out, note


def submit_sow(client, path: str | None = None, text: str | None = None, agent_id: str | None = None,
               budget_usdc=None) -> dict:
    """Parse a local SOW or pasted text, optionally creating a hash-bound draft."""
    has_path = isinstance(path, str) and path.strip()
    has_text = isinstance(text, str) and text.strip()
    if bool(has_path) == bool(has_text):
        return _error("INVALID_REQUEST", "pass exactly one of path (a local .pdf/.docx/.txt/.md file) or text",
                      "path")
    if agent_id is not None and str(agent_id).strip():
        agent_id, err = _check_agent_id(agent_id)
        if err:
            return err
    else:
        agent_id = None
    if budget_usdc is not None:
        budget_usdc, err = _check_usdc(budget_usdc, "budget_usdc")
        if err:
            return err
    try:
        if has_path:
            name, data, err = _read_sow(path)
            if err:
                return err
            scope = client.parse_sow(name, data)
        else:
            scope = client.parse_sow_text(text)
    except AgentListAPIError as exc:
        return _api_error(exc)

    source = scope.get("source") or {}
    source_document = {"filename": source.get("filename"), "sha256": source.get("sha256")}
    out = {"ok": True, "scope": {k: scope.get(k) for k in _SCOPE_KEYS}, "source_document": source_document,
           "engagement_created": False, "money_moved": False, "warnings": []}
    again = f"path={path!r}" if has_path else "text=<the same text>"
    if agent_id is None:
        query = scope.get("brief") or scope.get("outcome") or ""
        out["search_hint"] = {"query": query[:200], "category": scope.get("agent_category")}
        out["next_step"] = (
            "Nothing is created or paid yet. Show the human the parsed scope (outcome, milestones, deadline, "
            "budget and warnings). Then call search_agents, pick an agent with the human, and call "
            f"submit_sow({again}, agent_id=<AGT-...>) to draft the engagement bound to this document.")
        return out
    if not scope.get("outcome"):
        out["next_step"] = "The document has no clear outcome; ask the human what result they want, then call request_scope."
        return out
    budget = budget_usdc if budget_usdc is not None else scope.get("budget_usdc")
    if budget is None:
        out["next_step"] = ("The document states no budget; ask the human for a USDC budget and call "
                             f"submit_sow({again}, agent_id={agent_id!r}, budget_usdc=<amount>).")
        return out
    budget, err = _check_usdc(budget, "budget_usdc")
    if err:
        return err
    milestones, note = _sow_milestones(scope, Decimal(str(budget)))
    if note:
        out["warnings"].append(note)
    payload = {"agent_id": agent_id, "outcome": scope["outcome"], "budget_usdc": budget,
               "source_document": source_document}
    if milestones:
        payload["milestones"] = milestones
    deadline = (scope.get("deadline") or {}).get("date")
    if deadline:
        payload["deadline"] = deadline
    try:
        eng = _engagement(client.create_engagement(payload))
    except AgentListAPIError as exc:
        view = _api_error(exc)
        view["scope"] = out["scope"]
        return view
    eid = _engagement_id(eng)
    out.update({"engagement_created": True, "engagement_id": eid,
                "agent_id": eng.get("agent_id", agent_id), "status": eng.get("status"),
                "sow": eng.get("sow"), "sow_hash": eng.get("sow_hash"),
                "milestones": eng.get("milestones"), "screening": eng.get("screening"),
                "next_step": ("Nothing is paid yet. Show the human the SOW and total. If they agree, call "
                               f"hire(engagement_id={eid!r}, agent_id={agent_id!r}, confirm_amount_usdc={budget}); "
                               "that starts the protected human approval flow.")})
    return out


def hire(client, engagement_id: str, agent_id: str, confirm_amount_usdc) -> dict:
    agent_id, err = _check_agent_id(agent_id)
    if err:
        return err
    err = _require_text(engagement_id, "engagement_id")
    if err:
        return err
    confirm, err = _check_usdc(confirm_amount_usdc, "confirm_amount_usdc")
    if err:
        return err
    try:
        eng = _engagement(client.get_engagement(engagement_id))
    except AgentListAPIError as exc:
        return _api_error(exc)
    eng_agent = eng.get("agent_id")
    if eng_agent and agent_ids.is_valid(eng_agent) and agent_ids.normalize(eng_agent) != agent_id:
        return _error("AGENT_MISMATCH",
                      f"engagement {engagement_id} is scoped for {eng_agent}, not {agent_id}. "
                      "Confirm with the human which agent they mean.", "agent_id")
    try:
        approval = client.hire(engagement_id, confirm_amount_usdc=confirm, flow="device")
    except AgentListAPIError as exc:
        return _api_error(exc)
    return _approval_view(approval, engagement_id=engagement_id)


def release_milestone(client, engagement_id: str, milestone_index: int) -> dict:
    err = _require_text(engagement_id, "engagement_id")
    if err:
        return err
    if isinstance(milestone_index, bool) or not isinstance(milestone_index, int) or milestone_index < 0:
        return _error("INVALID_REQUEST", "milestone_index must be an integer >= 0", "milestone_index")
    try:
        approval = client.release_milestone(engagement_id, milestone_index, flow="device")
    except AgentListAPIError as exc:
        return _api_error(exc)
    view = _approval_view(approval, engagement_id=engagement_id)
    view["milestone_index"] = milestone_index
    return view


def submit_milestone(client, engagement_id: str, milestone_index: int, evidence: str) -> dict:
    err = _require_text(engagement_id, "engagement_id")
    if err:
        return err
    if isinstance(milestone_index, bool) or not isinstance(milestone_index, int) or milestone_index < 0:
        return _error("INVALID_REQUEST", "milestone_index must be an integer >= 0", "milestone_index")
    err = _require_text(evidence, "evidence")
    if err:
        return err
    try:
        body = client.submit_milestone(engagement_id, milestone_index, evidence.strip())
    except AgentListAPIError as exc:
        return _api_error(exc)
    return {"ok": True, **body,
            "next_step": "Show the submitted evidence to the human; call release_milestone only after acceptance."}


def get_job_chain(client, engagement_id: str) -> dict:
    err = _require_text(engagement_id, "engagement_id")
    if err:
        return err
    try:
        body = client.get_chain(engagement_id)
    except AgentListAPIError as exc:
        return _api_error(exc)
    return {"ok": True, **body}


def cancel_approval(client, approval_id: str) -> dict:
    err = _require_text(approval_id, "approval_id")
    if err:
        return err
    try:
        body = client.cancel_approval(approval_id)
    except AgentListAPIError as exc:
        return _api_error(exc)
    return {"ok": True, **body, "money_moved": False,
            "message": "Approval cancelled. No payment is authorized by this approval."}


def get_engagement_status(client, engagement_id: str, wait_seconds: float = 0, *,
                          sleep=time.sleep, clock=time.monotonic,
                          poll_interval: float = POLL_INTERVAL_SECONDS) -> dict:
    """Read the engagement; if an approval is open, poll it (which also lets the
    API advance device flows) until it is terminal or ``wait_seconds`` pass."""
    err = _require_text(engagement_id, "engagement_id")
    if err:
        return err
    try:
        wait = max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
    except (TypeError, ValueError):
        wait = 0.0
    deadline = clock() + wait
    while True:
        try:
            body = client.get_engagement(engagement_id)
            eng = _engagement(body)
            approval = _pick_approval(body, eng)
            if approval and approval.get("state") not in TERMINAL_APPROVAL_STATES:
                approval = client.get_approval(approval["approval_id"])
                if approval.get("state") in TERMINAL_APPROVAL_STATES:
                    # The executor may just have run: re-read ledger/status.
                    body = client.get_engagement(engagement_id)
                    eng = _engagement(body)
        except AgentListAPIError as exc:
            return _api_error(exc)
        approval_done = approval is None or approval.get("state") in TERMINAL_APPROVAL_STATES
        if approval_done or clock() + poll_interval > deadline:
            break
        sleep(poll_interval)
    return _status_view(body, eng, approval, engagement_id, timed_out=not approval_done and wait > 0)


def _status_view(body, eng, approval, engagement_id, *, timed_out: bool) -> dict:
    ledger = [e for e in (body.get("ledger") or eng.get("ledger") or []) if isinstance(e, dict)]
    # money_moved is about the approval being watched, so an earlier funding
    # never makes a pending release look paid.
    moved = _for_approval(_settled(ledger), approval)
    pending_tx = _for_approval([e for e in ledger if e.get("status") == "pending"], approval)
    status = eng.get("status")
    state = approval.get("state") if approval else None
    if approval is None:
        message = f"Engagement is '{status}'. No approval is open."
    elif state in TERMINAL_APPROVAL_STATES:
        message = f"Approval {approval.get('approval_id')} ended as '{state}'"
        if approval.get("failure_code"):
            message += f" ({approval['failure_code']})"
        message += f". Engagement is '{status}'."
    else:
        message = (f"Still waiting on the human: approval {approval.get('approval_id')} is '{state}'. "
                   f"Remind them of code {approval.get('user_code')} and call again with wait_seconds={MAX_WAIT_SECONDS}.")
    if moved:
        parts = []
        for e in moved:
            label = "simulated, no on-chain tx" if e.get("status") == "simulated" else "confirmed on-chain"
            parts.append(f"{e.get('kind')} {e.get('amount_micro')} micro-USDC ({label})")
        message += " Ledger shows for this approval: " + "; ".join(parts) + "."
    elif pending_tx:
        message += " A transaction is submitted but not yet confirmed; money has not settled yet."
    else:
        message += " No money has moved for this approval." if approval else " No money has moved."
    return {
        "ok": True,
        "engagement_id": _engagement_id(eng) or engagement_id,
        "status": status,
        "terminal": approval is None or state in TERMINAL_APPROVAL_STATES,
        "engagement_terminal": status in TERMINAL_ENGAGEMENT_STATUSES,
        "timed_out": timed_out,
        "approval": _nested(approval, engagement_id),
        "milestones": body.get("milestones") or eng.get("milestones"),
        "ledger": ledger,
        "money_moved": bool(moved),
        "money_moved_entries": moved,
        "settled_entries": _settled(ledger),
        "chain_url": body.get("chain_url") or eng.get("chain_url"),
        # The hired agent's mandate (the API only sends it to API-token callers);
        # pass it to subhire as mandate_token.
        "mandate_token": body.get("mandate_token") or eng.get("mandate_token"),
        "message": message,
    }


def _nested(approval, engagement_id):
    if approval is None:
        return None
    view = _approval_view(approval, engagement_id=engagement_id)
    view.pop("money_moved")  # the status view's top-level money_moved is authoritative
    view.pop("ok")
    return view


def subhire(client, parent_engagement_id: str, agent_id: str, budget_usdc, category: str,
            mandate_token: str, outcome: str | None = None) -> dict:
    """Sub-hire under the caller's mandate for ``parent_engagement_id``
    (POST /api/engagements/<id>/subhire, ``Authorization: Mandate <jwt>``).

    201: the child engagement was funded from the parent's escrow allocation
    (ledger-only); the reply carries the child's own ``mandate_token`` for the
    sub-agent. 202: screening asked for the root human, who approves on their
    phone; poll the child engagement with get_engagement_status."""
    agent_id, err = _check_agent_id(agent_id)
    if err:
        return err
    for value, field in ((parent_engagement_id, "parent_engagement_id"), (category, "category"),
                         (mandate_token, "mandate_token")):
        err = _require_text(value, field)
        if err:
            return err
    budget_usdc, err = _check_usdc(budget_usdc, "budget_usdc")
    if err:
        return err
    payload = {"agent_id": agent_id, "outcome": (outcome or "").strip() or f"{category} work for {parent_engagement_id}",
               "budget_usdc": budget_usdc, "category": category.strip()}
    try:
        body = client.subhire(parent_engagement_id, payload, mandate_token=mandate_token.strip())
    except AgentListAPIError as exc:
        return _api_error(exc)
    if isinstance(body, dict) and body.get("approval_id"):
        # 202: screening asked for the root human (ASK_HUMAN). The approval
        # belongs to the new child engagement, so that is the one to poll.
        child_id = body.get("child_engagement_id") or parent_engagement_id
        view = _approval_view(body, engagement_id=child_id)
        view.update(parent_engagement_id=parent_engagement_id, child_engagement_id=child_id)
        return view
    child = _engagement(body)
    capped = bool(body.get("capped"))
    message = ("Sub-hire funded within your mandate (a ledger allocation from the parent escrow, "
               "not an on-chain payment).")
    if capped:
        message += (f" Risk screening capped it: {body.get('allocated_micro')} of the requested "
                    f"{body.get('requested_micro')} micro-USDC was allocated. Tell the human.")
    message += " Give mandate_token only to the sub-hired agent; it is that agent's authority to sub-hire."
    return {"ok": True, "engagement_id": _engagement_id(child), "parent_engagement_id": parent_engagement_id,
            "agent_id": child.get("agent_id", agent_id), "status": child.get("status"),
            "depth": child.get("depth"), "sow_hash": child.get("sow_hash"),
            "allocated_micro": body.get("allocated_micro"), "requested_micro": body.get("requested_micro"),
            "capped": capped, "screening": body.get("screening"),
            "mandate": body.get("mandate") or child.get("mandate"),
            "mandate_token": body.get("mandate_token"), "chain_page_url": body.get("chain_page_url"),
            "money_moved": False, "message": message}
