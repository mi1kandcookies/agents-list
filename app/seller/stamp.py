"""Operator stamps: a verified human signs off on one exact agent configuration.

An agent's manifest (model, tools, MCP servers, skills, price range, payout
address and, optionally, the hash of its private runtime spec) is canonical
JSON, hashed like any action (docs/decisions/0001-custody-chain.md §1 and
its 2026-09-26 amendment). The operator stamps it with a
fresh World ID approval of kind ``manifest.publish`` bound to that hash, the
way an engineer stamps one specific set of drawings. Editing the manifest
does not touch the stamp, so the hashes stop matching and the listing needs a
re-stamp before it can be hired again.

Columns on ``agents``:
    manifest_json               the current manifest (canonical JSON)
    manifest_hash               hash of the manifest that was stamped
    manifest_stamped_at         when it was stamped
    manifest_stamp_approval_id  the manifest.publish approval (None for dev seeds)
    manifest_stamp_sub          the operator's pairwise World ID ``sub``

The stamped manifest itself is kept as a ``manifest_snapshot`` event on the
stamp approval, which is what the editor diffs against.

Hireable = stamped, the stamped hash equals the current manifest's hash, the
operator is not banned (bans follow the human, so all of their agents stop at
once) and the payout address did not screen REFUSE at onboarding.
``assert_hireable`` is the single gate the hire paths call.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.approvals import actions
from app.approvals.executors import ExecutionResult, executor
from app.engagements.service import EngagementError
from app.extensions import db

log = logging.getLogger("agents_list.stamp")

MANIFEST_VERSION = 1
LIST_FIELDS = ("tools", "mcp_servers", "skills")
MAX_ITEMS = 50
MAX_TEXT = 200
# Marks stamps written by `flask seed-stamps`; never a real World ID subject.
DEV_STAMP_SUB = "simulated:dev-seed"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SPEC_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")

CODES = ("NOT_STAMPED", "RESTAMP_REQUIRED", "OPERATOR_BANNED", "PAYEE_REFUSED")
REASONS = {
    "NOT_STAMPED": "The operator has not stamped this agent's configuration yet.",
    "RESTAMP_REQUIRED": "The configuration changed after it was stamped; the operator must re-stamp it.",
    "OPERATOR_BANNED": "The operator behind this agent is banned.",
    "PAYEE_REFUSED": "The payout address was refused (or not screened) at onboarding.",
}


class ManifestError(ValueError):
    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


class NotHireable(EngagementError):
    """The agent cannot be hired right now; ``code`` is one of CODES."""

    def __init__(self, code: str, message: Optional[str] = None):
        super().__init__(message or REASONS[code], code, 409, "agent_id")


# ── manifest ───────────────────────────────────────────────────────────────

def _text(value, field: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > MAX_TEXT:
        raise ManifestError(f"{field} is longer than {MAX_TEXT} characters", field)
    return text


def _items(value, field: str) -> list[str]:
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    items = sorted({_text(v, field) for v in (value or [])} - {""})
    if len(items) > MAX_ITEMS:
        raise ManifestError(f"{field} has more than {MAX_ITEMS} entries", field)
    return items


def _micro(value, field: str) -> int:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ManifestError(f"{field} is required", field)
    try:
        micro = Decimal(str(value).strip()) * 1_000_000
    except (InvalidOperation, ValueError):
        raise ManifestError(f"{field} must be a decimal USDC amount", field) from None
    if not micro.is_finite() or micro < 0 or micro != micro.to_integral_value():
        raise ManifestError(f"{field} must be >= 0 with at most 6 decimals", field)
    return int(micro)


def _spec_hash(value) -> Optional[str]:
    """Lowercase ``0x`` + 64 hex, or None when absent or blank."""
    digest = value.strip().lower() if isinstance(value, str) else value
    if digest is None or digest == "":
        return None
    if not isinstance(digest, str) or not _SPEC_HASH_RE.fullmatch(digest):
        raise ManifestError("spec hash must be 0x followed by 64 hex characters", "spec_hash")
    return digest


def build_manifest(agent, *, model, tools=(), mcp_servers=(), skills=(), price_min_usdc=None,
                   price_max_usdc=None, payout_address=None, price_min_micro=None,
                   price_max_micro=None, spec_hash=None) -> dict:
    """Validate and normalize a manifest for ``agent``. Lists are de-duplicated
    and sorted so the hash does not depend on entry order; money is integer
    micro-USDC; the address is lowercase. ``spec_hash`` (optional, lowercase
    ``0x`` + 64 hex) commits to the agent's private runtime spec; when absent
    the key is left out, so manifests without one hash exactly as before."""
    model = _text(model, "model")
    if not model:
        raise ManifestError("model is required", "model")
    lo = price_min_micro if price_min_micro is not None else _micro(price_min_usdc, "price_min")
    hi = price_max_micro if price_max_micro is not None else _micro(price_max_usdc, "price_max")
    if hi < lo:
        raise ManifestError("the maximum price must be at least the minimum", "price_max")
    payout = str(payout_address or "").strip()
    if not _ADDRESS_RE.match(payout):
        raise ManifestError("payout address must be a 0x-prefixed 20-byte hex address",
                            "payout_address")
    spec = _spec_hash(spec_hash)
    manifest = {
        "v": MANIFEST_VERSION, "agent_id": agent.public_id, "model": model,
        "tools": _items(tools, "tools"), "mcp_servers": _items(mcp_servers, "mcp_servers"),
        "skills": _items(skills, "skills"),
        "price_min_micro": int(lo), "price_max_micro": int(hi),
        "payout_address": payout.lower(),
    }
    if spec is not None:
        manifest["spec_hash"] = spec
    return manifest


def manifest_hash(manifest: dict) -> str:
    return "0x" + hashlib.sha256(actions.canonical(manifest)).hexdigest()


def current_manifest(agent) -> Optional[dict]:
    try:
        data = json.loads(agent.manifest_json or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def default_manifest(agent) -> dict:
    """A starting manifest from the listing's own fields (not validated)."""
    model = " | ".join(p for p in (agent.model_provider, agent.model_name) if p)
    payout = next((a for a in (agent.payout_address, agent.deployer_wallet)
                   if a and _ADDRESS_RE.match(a)), "")
    return {
        "v": MANIFEST_VERSION, "agent_id": agent.public_id, "model": model,
        "tools": [], "mcp_servers": [], "skills": sorted(set(agent.capabilities or [])),
        "price_min_micro": _float_micro(agent.min_price),
        "price_max_micro": _float_micro(agent.max_price),
        "payout_address": payout.lower(),
    }


def _float_micro(value) -> int:
    try:
        return int((Decimal(str(value or 0)) * 1_000_000).to_integral_value())
    except InvalidOperation:
        return 0


def save_manifest(agent, manifest: dict) -> str:
    """Store ``manifest`` as the agent's current configuration and return its
    hash. The stamp is left alone: a different hash means re-stamp required."""
    agent.manifest_json = actions.canonical(manifest).decode("utf-8")
    db.session.flush()
    return manifest_hash(manifest)


def stamped_manifest(agent) -> Optional[dict]:
    """The manifest that was stamped, from the stamp approval's snapshot."""
    if not agent.manifest_stamp_approval_id:
        return None
    from app.models import ApprovalEvent
    ev = (ApprovalEvent.query
          .filter_by(approval_id=agent.manifest_stamp_approval_id, event="manifest_snapshot")
          .order_by(ApprovalEvent.id.desc()).first())
    manifest = (ev.detail or {}).get("manifest") if ev else None
    return manifest if isinstance(manifest, dict) else None


def diff(old: Optional[dict], new: Optional[dict]) -> list[dict]:
    """Field-level changes ``[{field, old, new}]`` between two manifests."""
    old, new = old or {}, new or {}
    return [{"field": k, "old": old.get(k), "new": new.get(k)}
            for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)]


# ── hireability ────────────────────────────────────────────────────────────

@dataclass
class StampStatus:
    ok: bool
    code: Optional[str]
    reason: str
    current_hash: Optional[str]
    stamped_hash: Optional[str]
    simulated: bool = False


def onboarding_screening(agent, address: Optional[str] = None):
    """The latest ``payee.onboard`` screening of the agent's payout address."""
    from app.models import Screening
    address = (address or agent.payout_address or "").lower()
    if not address:
        return None
    return (Screening.query
            .filter_by(hop="payee.onboard", agent_id=agent.id, chain_address=address)
            .order_by(Screening.created_at.desc()).first())


def stamp_status(agent) -> StampStatus:
    from app.engagements.service import payee_address
    from app.humans import service as humans
    manifest = current_manifest(agent)
    current = manifest_hash(manifest) if manifest else None
    stamped = agent.manifest_hash if agent.manifest_stamped_at else None
    simulated = agent.manifest_stamp_sub == DEV_STAMP_SUB

    def status(code):
        return StampStatus(code is None, code, REASONS.get(code, "Operator-stamped configuration."),
                           current, stamped, simulated)

    if not stamped or not agent.manifest_stamp_sub:
        return status("NOT_STAMPED")
    operator = humans.get_by_sub(agent.manifest_stamp_sub)
    if operator is not None and operator.banned:
        return status("OPERATOR_BANNED")
    if current != stamped or payee_address(agent) != (manifest or {}).get("payout_address"):
        return status("RESTAMP_REQUIRED")
    screening = onboarding_screening(agent)
    # A real stamp whose payout was never screened fails closed.
    if (screening is None and not simulated) or (screening is not None
                                                 and screening.verdict == "REFUSE"):
        return status("PAYEE_REFUSED")
    return status(None)


def assert_hireable(agent) -> None:
    """Raise NotHireable (NOT_STAMPED, RESTAMP_REQUIRED, OPERATOR_BANNED,
    PAYEE_REFUSED) unless ``agent`` can be hired right now."""
    result = stamp_status(agent)
    if not result.ok:
        raise NotHireable(result.code)


def agents_operated_by(sub: str) -> list:
    from app.models import Agent
    return Agent.query.filter_by(manifest_stamp_sub=sub).order_by(Agent.id).all()


# ── stamping ───────────────────────────────────────────────────────────────

def request_stamp(agent, *, flow: str = "web"):
    """Screen the payout address (payee.onboard) and create the
    ``manifest.publish`` approval for the agent's current manifest.

    The approval is bound to the manifest hash and payout address. The
    screening runs here rather than in the executor because the screener
    commits its verdict, which must not happen inside consume()'s savepoint.
    Its verdict is deliberately not bound into the action: a REFUSE must not
    stop the operator from stamping; it blocks the listing instead
    (PAYEE_REFUSED). The manifest and verdict id are kept as a
    ``manifest_snapshot`` event for diffs and audit."""
    from app.approvals import service as approvals
    from app.models import ApprovalEvent
    manifest = current_manifest(agent)
    if manifest is None:
        raise ManifestError("save the manifest before stamping it")
    screening = _screen_payee(agent, manifest["payout_address"])
    db.session.commit()
    approval = approvals.create_approval(
        "manifest.publish",
        {"manifest_hash": manifest_hash(manifest), "payee_agent_id": agent.public_id,
         "payee_address": manifest["payout_address"]},
        flow=flow, agent_id=agent.id)
    db.session.add(ApprovalEvent(approval_id=approval.id, event="manifest_snapshot",
                                 detail={"manifest": manifest, "screening_id": screening.id,
                                         "screening_verdict": screening.verdict}))
    db.session.commit()
    return approval


@executor("manifest.publish")
def execute_publish(approval, action: dict) -> ExecutionResult:
    """Record the stamp and issue the agent's name. A REFUSE onboarding
    verdict keeps the stamp but blocks the listing (PAYEE_REFUSED); a names
    failure is never fatal."""
    from app.models import Agent
    agent = db.session.get(Agent, approval.agent_id) if approval.agent_id else None
    if agent is None or action.get("payee_agent_id") != agent.public_id:
        return ExecutionResult(ok=False, summary="agent not found")
    manifest = current_manifest(agent)
    if manifest is None or manifest_hash(manifest) != action.get("manifest_hash"):
        return ExecutionResult(ok=False, summary="manifest changed since approval")
    if manifest.get("payout_address") != action.get("payee_address"):
        return ExecutionResult(ok=False, summary="payout address changed since approval")
    operator = agent.manifest_stamp_sub
    if operator and operator != DEV_STAMP_SUB and operator != approval.human_sub:
        return ExecutionResult(ok=False, summary="this agent is operated by another human")

    agent.manifest_hash = action["manifest_hash"]
    agent.manifest_stamped_at = datetime.now(timezone.utc)
    agent.manifest_stamp_approval_id = approval.id
    agent.manifest_stamp_sub = approval.human_sub
    agent.payout_address = action["payee_address"]
    db.session.flush()

    agent_id = agent.id
    summary = f"Stamped manifest {action['manifest_hash'][:18]}…"
    screening = onboarding_screening(agent, action["payee_address"])
    if screening is None or screening.verdict == "REFUSE":
        summary += "; payout address refused by screening, listing blocked"
    result = ExecutionResult(ok=True, summary=summary,
                             redirect=f"/seller/agents/{agent_id}/manifest")
    # Last on purpose: the names hook commits, after which nothing here may
    # touch the session (consume() still holds its savepoint context).
    try:
        from app.names.service import on_agent_published
        on_agent_published(agent)
    except Exception:  # noqa: BLE001 - names never block a stamp
        log.exception("names hook failed for agent %s", agent_id)
    return result


def _screen_payee(agent, address: str):
    """Run payee.onboard screening and make sure the verdict is stored with
    the agent; returns the Screening row. Any failure is recorded as a
    fail-closed REFUSE."""
    from app.common.ids import new_id
    from app.models import Screening
    try:
        from app.screening.service import screen
        v = dict(screen("payee.onboard", chain_address=address, amount_micro=0,
                        agent_id=agent.public_id))
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.warning("payee.onboard screening failed for agent %s: %s", agent.id, str(exc)[:200])
        v = {"id": new_id("SCR"), "verdict": "REFUSE", "fail_closed": True, "provider": "none",
             "reasons": [{"code": "SCREENING_ERROR", "message": "screening failed",
                          "source": "agents-list"}]}
    row = db.session.get(Screening, v.get("id")) if v.get("id") else None
    if row is None:
        signals = v.get("signals") or {}
        row = Screening(
            id=v.get("id") or new_id("SCR"), hop="payee.onboard", agent_id=agent.id,
            chain_address=address.lower(),
            screened_address=(v.get("subject") or {}).get("screened_address"),
            network=(v.get("subject") or {}).get("network"),
            verdict=v.get("verdict") if v.get("verdict") in ("PAY", "CAP", "REFUSE", "ASK_HUMAN")
            else "REFUSE",
            cap_micro=v.get("cap_micro"), reasons=v.get("reasons") or [],
            toxic_score=signals.get("toxic_score"), traits=signals.get("traits") or [],
            risk_group=signals.get("risk_group"), raw=v, provider=v.get("provider") or "",
            fail_closed=bool(v.get("fail_closed")), latency_ms=v.get("latency_ms"),
        )
        db.session.add(row)
    elif row.agent_id is None:
        row.agent_id = agent.id
    db.session.flush()
    return row


# ── development seed ───────────────────────────────────────────────────────

def dev_stamp(agent) -> None:
    """Stamp ``agent``'s current (or default) manifest without a World ID
    approval. Development only: the stamp is marked with DEV_STAMP_SUB and
    has no approval id. Callers must refuse in production."""
    manifest = current_manifest(agent)
    if manifest is None:
        base = default_manifest(agent)
        manifest = build_manifest(
            agent, model=base["model"] or "unspecified", tools=base["tools"],
            mcp_servers=base["mcp_servers"], skills=base["skills"],
            price_min_micro=base["price_min_micro"],
            price_max_micro=max(base["price_max_micro"], base["price_min_micro"]),
            payout_address=base["payout_address"])
        save_manifest(agent, manifest)
    agent.manifest_hash = manifest_hash(manifest)
    agent.manifest_stamped_at = datetime.now(timezone.utc)
    agent.manifest_stamp_approval_id = None
    agent.manifest_stamp_sub = DEV_STAMP_SUB
    agent.payout_address = manifest["payout_address"]
    db.session.flush()


def seed_stamps_refusal(app) -> Optional[str]:
    """Why ``flask seed-stamps`` must not run here, or None. Refuses in the
    production config and whenever a real World ID client is configured
    outside debug/testing."""
    import os
    env = str(app.config.get("ENV_NAME") or os.environ.get("FLASK_ENV") or "").lower()
    if env == "production":
        return "dev seed stamps are never written in production"
    if (os.environ.get("WORLD_CLIENT_ID") or "").strip() and not (app.debug or app.testing):
        return "WORLD_CLIENT_ID is set; operators must stamp with World ID"
    return None


def seed_demo_stamps() -> list[str]:
    """Dev-stamp the demo agents from ``flask seed-demo``. Returns their names."""
    from app.demo_seed import DEMO_AGENTS, DEMO_OPERATOR
    from app.models import Agent
    names = {spec["name"] for spec in DEMO_AGENTS}
    done = []
    for agent in Agent.query.filter(Agent.name.in_(names)).order_by(Agent.id).all():
        if agent.seller != DEMO_OPERATOR:
            continue
        if agent.manifest_stamp_sub and agent.manifest_stamp_sub != DEV_STAMP_SUB:
            continue  # a real operator stamp is never overwritten
        dev_stamp(agent)
        done.append(agent.name)
    db.session.commit()
    return done
