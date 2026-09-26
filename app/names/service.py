"""Name lifecycle hooks. Each hook persists an ``EnsName`` row and pushes it to
the names sidecar. Sidecar problems never propagate: the row is left
``pending`` (sidecar not configured, or parent name not active yet) or
``failed`` (the call failed) and can be retried via ``retry(name)``.

    on_agent_published(agent)          <agent>.<root>
    on_engagement_funded(engagement)   <eng-id>.<agent>.<root>   expiry = SOW deadline
    on_subhire(child_engagement)       <child-id>.<parent job>   wildcard records
    on_settled(engagement)             revoke the job / sub-job name

Hooks commit the session so the name state survives the caller's request.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.extensions import db
from app.models import Agent, Engagement, EnsName
from app.names.client import SidecarError, get_client

log = logging.getLogger("agents_list.names")

SETTLED = "settled"                   # records["status"] once the job is settled
DEFAULT_JOB_TTL = timedelta(days=30)  # when an engagement has no deadline
_LABEL_RE = re.compile(r"[^a-z0-9]+")


def root_name() -> str:
    return current_app.config.get("ENS_ROOT_NAME") or "agentslist-app.eth"


def namehash(name: str) -> str:
    from eth_utils import keccak
    node = b"\x00" * 32
    for label in reversed(name.split(".")):
        node = keccak(node + keccak(text=label))
    return "0x" + node.hex()


def slug(text: str) -> str:
    return _LABEL_RE.sub("-", (text or "").lower()).strip("-")[:40].strip("-")


def _unix(dt: datetime) -> int:
    if dt.tzinfo is None:           # stored as naive UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _get_or_create(name: str, **fields) -> EnsName:
    row = db.session.get(EnsName, name)
    if row is None:
        row = EnsName(name=name, node=namehash(name), status="pending", records={}, tx_hashes=[], **fields)
        db.session.add(row)
    return row


def _root_row() -> EnsName:
    return _get_or_create(root_name(), kind="root")


def _commit() -> None:
    try:
        db.session.commit()
    except Exception:  # never break the caller over name bookkeeping
        db.session.rollback()
        log.exception("names: could not persist name state")


# ── Hooks ─────────────────────────────────────────────────────────────────────

def on_agent_published(agent: Agent) -> EnsName | None:
    try:
        row = _agent_row(agent)
        _push(row)
        return row
    except Exception:
        log.exception("names: on_agent_published failed for agent %s", getattr(agent, "id", None))
        return None
    finally:
        _commit()


def on_engagement_funded(engagement: Engagement) -> EnsName | None:
    try:
        agent_row = _agent_row(engagement.agent)
        if agent_row.status != "active":
            _push(agent_row)
        row = _get_or_create(f"{engagement.id.lower()}.{agent_row.name}", kind="job",
                             parent_name=agent_row.name)
        row.engagement_id = engagement.id
        row.agent_id = engagement.agent_id
        row.expiry = _unix(engagement.deadline_at) if engagement.deadline_at else \
            _unix(datetime.now(timezone.utc) + DEFAULT_JOB_TTL)
        from chain.config import get_address
        row.records = _clean({
            "sow_hash": engagement.sow_hash,
            "escrow": get_address("EscrowPayment"),
            "mandate": engagement.mandate_id,
            "status": "funded",
        })
        _push(row)
        return row
    except Exception:
        log.exception("names: on_engagement_funded failed for %s", getattr(engagement, "id", None))
        return None
    finally:
        _commit()


def on_subhire(child_engagement: Engagement) -> EnsName | None:
    try:
        parent_row = _engagement_row(child_engagement.parent_engagement_id)
        if parent_row is None or parent_row.kind != "job":
            log.info("names: sub-hire %s has no parent job name; skipped", child_engagement.id)
            return None
        row = _get_or_create(f"{child_engagement.id.lower()}.{parent_row.name}", kind="subjob",
                             parent_name=parent_row.name)
        row.engagement_id = child_engagement.id
        row.agent_id = child_engagement.agent_id
        # Sub-job records live on the job's resolver, so they can't outlive the job.
        own = _unix(child_engagement.deadline_at) if child_engagement.deadline_at else None
        candidates = [e for e in (own, parent_row.expiry) if e]
        row.expiry = min(candidates) if candidates else None
        row.records = _clean({
            "sow_hash": child_engagement.sow_hash,
            "mandate": child_engagement.mandate_id,
            "status": "funded",
        })
        _push(row)
        return row
    except Exception:
        log.exception("names: on_subhire failed for %s", getattr(child_engagement, "id", None))
        return None
    finally:
        _commit()


def on_settled(engagement: Engagement) -> EnsName | None:
    try:
        row = _engagement_row(engagement.id)
        if row is None:
            return None
        row.records = {**(row.records or {}), "status": SETTLED}
        _push(row)
        return row
    except Exception:
        log.exception("names: on_settled failed for %s", getattr(engagement, "id", None))
        return None
    finally:
        _commit()


def retry(name: str) -> EnsName | None:
    """Re-push a pending/failed name (and its not-yet-active parent). Active
    and revoked names are left as they are."""
    row = db.session.get(EnsName, name)
    if row is None:
        return None
    try:
        if row.status in ("pending", "failed"):
            _push(row, with_parent=True)
    finally:
        _commit()
    return row


# ── Internals ─────────────────────────────────────────────────────────────────

def _clean(records: dict) -> dict:
    return {k: v for k, v in records.items() if v not in (None, "")}


def _engagement_row(engagement_id: str | None) -> EnsName | None:
    if not engagement_id:
        return None
    return (EnsName.query.filter(EnsName.engagement_id == engagement_id,
                                 EnsName.kind.in_(("job", "subjob")))
            .order_by(EnsName.updated_at.desc()).first())


def _manifest(agent: Agent) -> dict:
    try:
        data = json.loads(agent.manifest_json or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _agent_row(agent: Agent) -> EnsName:
    existing = (EnsName.query.filter_by(agent_id=agent.id, kind="agent")
                .filter(EnsName.status != "revoked").first())
    root = _root_row()
    if existing is not None:
        return existing
    label = slug(agent.name) or agent.public_id.lower()
    name = f"{label}.{root.name}"
    taken = db.session.get(EnsName, name)
    if taken is not None and taken.agent_id != agent.id:
        name = f"{label[:30]}-{agent.public_id.replace('-', '').lower()[3:11]}.{root.name}"
    row = _get_or_create(name, kind="agent", parent_name=root.name)
    if row.status == "revoked":
        row.status = "pending"       # re-issue; the sidecar deploys fresh proxies
    row.agent_id = agent.id
    manifest = _manifest(agent)
    endpoints = manifest.get("endpoints") if isinstance(manifest.get("endpoints"), dict) else {}
    row.records = _clean({
        "context": (agent.description or "")[:1000],
        "mcp": manifest.get("mcp_endpoint") or endpoints.get("mcp"),
        "erc8004_agent_id": manifest.get("erc8004_agent_id"),
    })
    return row


def _grantee(row: EnsName) -> str | None:
    if row.kind != "job" or not row.engagement_id:
        return None
    engagement = db.session.get(Engagement, row.engagement_id)
    agent = engagement.agent if engagement else None
    return (agent.payout_address or agent.deployer_wallet) if agent else None


def _push(row: EnsName, *, with_parent: bool = False) -> None:
    """Send the row's intended state to the sidecar and record the outcome.
    ``with_parent`` (retry only) first re-pushes a pending/failed parent."""
    client = get_client()
    label = row.name.split(".", 1)[0]
    settling = (row.records or {}).get("status") == SETTLED
    if row.status == "revoked" or (row.status == "active" and not settling):
        return
    if settling and not row.tx_hashes:
        row.status = "revoked"      # never reached the chain; nothing to undo
        return
    if not client.configured:
        row.status = "pending"
        return
    parent = db.session.get(EnsName, row.parent_name) if row.parent_name else None
    if not settling and row.kind in ("job", "subjob"):
        if with_parent and parent is not None and parent.status in ("pending", "failed"):
            _push(parent, with_parent=True)
        if parent is None or parent.status != "active":
            row.status = "pending"
            return
    try:
        if settling:
            result = client.revoke(row.name, grantee=_grantee(row))
        elif row.kind == "agent":
            agent = db.session.get(Agent, row.agent_id)
            result = client.create_agent(agent_public_id=agent.public_id, label=label, records=row.records)
        elif row.kind == "job":
            result = client.create_job(parent=row.parent_name, label=label, expiry=row.expiry,
                                       records=row.records, grantee=_grantee(row))
        elif row.kind == "subjob":
            result = client.create_subjob(parent=row.parent_name, label=label, expiry=row.expiry,
                                          records=row.records)
        else:
            return          # the root is set up by an operator on the sidecar
    except SidecarError as exc:
        row.status = "failed"
        log.warning("names: %s %s failed: %s (%s)", "revoke" if settling else "issue", row.name, exc, exc.code)
        return
    hashes = [t.get("hash") for t in result.get("txs", []) if isinstance(t, dict) and t.get("hash")]
    row.tx_hashes = list(dict.fromkeys([*(row.tx_hashes or []), *hashes]))
    row.owner = result.get("owner") or row.owner
    if result.get("expiry"):
        row.expiry = int(result["expiry"])
    if settling:
        row.status = "revoked"
        for child in EnsName.query.filter_by(parent_name=row.name).all():
            child.status = "revoked"
        return
    row.status = "active"
    if row.kind == "agent":
        _root_row().status = "active"   # the sidecar only issues agents under a live root
        agent = db.session.get(Agent, row.agent_id)
        if agent is not None:
            agent.ens_name = row.name
    else:
        engagement = db.session.get(Engagement, row.engagement_id) if row.engagement_id else None
        if engagement is not None:
            engagement.ens_name = row.name


def tree(root: str | None = None) -> dict:
    """Nested ``{name, kind, status, …, children}`` for ``root`` from the database."""
    root = root or root_name()
    rows = EnsName.query.filter((EnsName.name == root) | EnsName.name.like(f"%.{root}")).all()
    by_parent: dict[str | None, list[EnsName]] = {}
    for r in rows:
        by_parent.setdefault(r.parent_name, []).append(r)

    def node(r: EnsName | None, name: str) -> dict:
        kids = sorted(by_parent.get(name, []), key=lambda x: x.name)
        base = _row_dict(r) if r is not None else {"name": name, "kind": "root", "status": "pending"}
        return {**base, "children": [node(k, k.name) for k in kids]}

    top = next((r for r in rows if r.name == root), None)
    return node(top, root)


def _row_dict(r: EnsName) -> dict:
    return {
        "name": r.name, "node": r.node, "kind": r.kind, "status": r.status,
        "parent_name": r.parent_name, "engagement_id": r.engagement_id, "agent_id": r.agent_id,
        "owner": r.owner, "expiry": r.expiry,
        "expires_at": datetime.fromtimestamp(r.expiry, timezone.utc).isoformat() if r.expiry else None, "records": r.records or {}, "tx_hashes": r.tx_hashes or [],
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
