"""Name lifecycle hooks. Each hook persists an ``EnsName`` row and pushes it to
the names sidecar. Sidecar problems never propagate: the row is left
``pending`` (sidecar not configured, or parent name not active yet) or
``failed`` (the call failed) and can be retried via ``retry(name)``.

    on_agent_published(agent)          <agent>.<root>
    on_engagement_funded(engagement)   <eng-id>.<agent>.<root>   expiry = SOW deadline
    on_subhire(child_engagement)       <child-id>.<parent job>   wildcard records
    on_settled(engagement)             revoke the job / sub-job name

Hooks commit the session so the name state survives the caller's request.

``resolve_payee(agent)`` answers where an agent is paid, and whether ENS
vouched for it (see its docstring).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import current_app, has_app_context

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
    return _LABEL_RE.sub("-", (text or "").lower()).strip("-")[:LABEL_MAX].strip("-")


# ── Agent labels ──────────────────────────────────────────────────────────────
# An operator picks the <label> of <label>.<root> when listing an agent
# (``Agent.ens_label``); without one the label is ``slug(agent.name)``.

LABEL_MAX = 40                      # slug() cap; leaves room under the 63-byte DNS limit
_VALID_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
ENS_APP_URL = "https://sepolia.app.ens.domains"
# Labels kept for the platform's own use under the root name.
RESERVED_LABELS = frozenset({
    "admin", "agent", "agents", "api", "app", "docs", "escrow", "help", "job", "jobs",
    "mail", "names", "operator", "platform", "root", "status", "support", "treasury", "www",
})


class LabelError(ValueError):
    def __init__(self, message: str, code: str = "LABEL_INVALID"):
        super().__init__(message)
        self.message, self.code = message, code


def normalize_label(text) -> str:
    return str(text or "").strip().lower()


def label_error(label: str) -> LabelError | None:
    """Why ``label`` is not a valid agent label, or None. The character rules
    are the sidecar's (ens/lib/names.mjs ``assertLabel``: a-z, 0-9 and inner
    hyphens), the length cap is ``slug``'s, and ENSIP-15 rejects ``--`` in
    the third and fourth positions."""
    if not label:
        return LabelError("choose a name for your agent", "LABEL_REQUIRED")
    if len(label) > LABEL_MAX:
        return LabelError(f"use at most {LABEL_MAX} characters", "LABEL_TOO_LONG")
    if not _VALID_LABEL_RE.match(label):
        return LabelError("use lowercase letters, digits and hyphens, not at the start or end",
                          "LABEL_INVALID")
    if label[2:4] == "--":
        return LabelError("a hyphen pair cannot be the third and fourth characters",
                          "LABEL_INVALID")
    return None


def label_taken(label: str, agent_id: int | None = None) -> LabelError | None:
    """Why ``label`` is not free for agent ``agent_id`` (None for a new
    listing): reserved, issued to another agent, or chosen by another
    listing. Returns None when it is free."""
    root = root_name()
    if label in RESERVED_LABELS or label == root.split(".", 1)[0]:
        return LabelError(f"{label}.{root} is reserved", "LABEL_RESERVED")
    row = db.session.get(EnsName, f"{label}.{root}")
    if row is not None and (row.agent_id is None or row.agent_id != agent_id):
        return LabelError(f"{label}.{root} is already taken", "LABEL_TAKEN")
    claimed = Agent.query.filter(Agent.ens_label == label)
    if agent_id is not None:
        claimed = claimed.filter(Agent.id != agent_id)
    if claimed.first() is not None:
        return LabelError(f"{label}.{root} is already taken", "LABEL_TAKEN")
    return None


def check_label(text, agent_id: int | None = None) -> dict:
    """Availability of ``text`` as an agent label: ``{label, name, root,
    available}`` plus ``code`` and ``error`` when it is not available."""
    label = normalize_label(text)
    root = root_name()
    problem = label_error(label) or label_taken(label, agent_id)
    out = {"label": label, "name": f"{label}.{root}" if label else None, "root": root,
           "available": problem is None}
    if problem is not None:
        out.update(code=problem.code, error=problem.message)
    return out


def agent_label(agent: Agent) -> str:
    """The label ``agent`` is (or will be) issued under."""
    return agent.ens_label or slug(agent.name) or agent.public_id.lower()


def agent_name_row(agent: Agent) -> EnsName | None:
    return (EnsName.query.filter_by(agent_id=agent.id, kind="agent")
            .filter(EnsName.status != "revoked").first())


def _label_locked(row: EnsName | None) -> bool:
    """A name is locked once it is active, has reached the chain, or has job
    names under it."""
    if row is None:
        return False
    return (row.status not in ("pending", "failed") or bool(row.tx_hashes)
            or EnsName.query.filter_by(parent_name=row.name).first() is not None)


def set_agent_label(agent: Agent, text) -> str:
    """Validate and store the operator's label for ``agent``. Raises
    LabelError (LABEL_LOCKED once the name is locked). A pending or failed
    name that never reached the chain is replaced; a stamped agent is then
    re-published under the new label (which commits the session)."""
    label = normalize_label(text)
    problem = label_error(label) or label_taken(label, agent.id)
    if problem is not None:
        raise problem
    row = agent_name_row(agent)
    if row is not None and row.name.split(".", 1)[0] == label:
        agent.ens_label = label
        return label
    if _label_locked(row):
        raise LabelError(f"{row.name} is already issued and cannot be renamed", "LABEL_LOCKED")
    agent.ens_label = label
    if row is not None:
        db.session.delete(row)
        db.session.flush()
        if agent.manifest_stamped_at:
            on_agent_published(agent)
    return label


def agent_name_view(agent: Agent) -> dict:
    """What the operator's manifest page shows about the agent's name."""
    row = agent_name_row(agent)
    name = row.name if row is not None else f"{agent_label(agent)}.{root_name()}"
    tx = (row.tx_hashes or [None])[-1] if row is not None else None
    return {
        "name": name, "label": name.split(".", 1)[0], "root": root_name(),
        "status": row.status if row is not None else "unissued",
        "editable": not _label_locked(row), "tx_hash": tx,
        "ens_app_url": f"{ENS_APP_URL}/{name}" if tx else None,
    }


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
    """Issue the agent's name, or update an active one whose records changed."""
    try:
        row = _agent_row(agent)
        _refresh(row, agent)
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
            _refresh(agent_row, engagement.agent)
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


def publish_agent(agent: Agent) -> tuple[EnsName, str | None, int]:
    """Issue ``agent``'s name now, or re-send an active one so the sidecar
    brings its records, grants and parent link up to date (bulk publish).
    Returns the row, why the push failed (or None), and the new tx count."""
    try:
        row = _agent_row(agent)
        before = len(row.tx_hashes or [])
        error = _refresh(row, agent, force=True)
        return row, error, len(row.tx_hashes or []) - before
    finally:
        _commit()


def plan_agent(agent: Agent) -> tuple[str, str, dict]:
    """The name ``publish_agent`` would use, its status and the records it
    would send. Persists nothing."""
    try:
        row = _agent_row(agent)
        return row.name, row.status, _agent_records(agent)
    finally:
        db.session.rollback()


def retry(name: str) -> tuple[EnsName | None, str | None]:
    """Re-push a pending/failed name (and its not-yet-active parent), or
    re-send an active agent name so a failed record update lands. Revoked
    names are left as they are. Returns the row and why the push failed."""
    row = db.session.get(EnsName, name)
    if row is None:
        return None, None
    error = None
    try:
        if row.kind == "agent" and row.status in ("pending", "failed", "active"):
            error = _push_agent(row)
        elif row.status in ("pending", "failed"):
            error = _push(row, with_parent=True)
    finally:
        _commit()
    return row, error


def unhireable_reason(agent: Agent) -> str | None:
    """Why the app would not hire ``agent`` right now (UNLISTED, a stamp
    status code, or NO_PAYOUT), or None. publish-agents issues new names to
    hireable agents only."""
    from app.engagements.service import payee_address
    from app.seller.stamp import stamp_status
    from app.services import is_listed
    if not is_listed(agent):
        return "UNLISTED"
    stamp = stamp_status(agent)
    if not stamp.ok:
        return stamp.code
    return None if payee_address(agent) else "NO_PAYOUT"


# ── Payee resolution ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Payee:
    address: str | None          # lowercase 0x address; None when the agent has none
    source: str                  # "ens" | "profile"
    ens_name: str | None = None


class PayeeError(Exception):
    """Payee refused. ``PAYEE_MISMATCH`` (403): the ENS record names another
    address than the profile. ``PAYEE_UNRESOLVED`` (503): the lookup failed."""

    def __init__(self, message: str, code: str = "PAYEE_MISMATCH", status: int = 403):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def get_resolver():
    """ENS reader with ``payout_address(name)``: ``app.extensions["ens_resolver"]``
    (tests), else the Universal Resolver when ``ENS_RESOLVE_PAYEES`` is on,
    else None."""
    if not has_app_context():
        return None
    override = current_app.extensions.get("ens_resolver")
    if override is not None:
        return override
    if not current_app.config.get("ENS_RESOLVE_PAYEES"):
        return None
    from chain.ens_v2 import UniversalResolver
    return UniversalResolver.from_env(current_app.config.get("ENS_UNIVERSAL_RESOLVER") or None)


def resolve_payee(agent: Agent) -> Payee:
    """Where ``agent`` is paid.

    With a resolver configured and an active agent name, the name's x402
    payout record (``chain.ens_v2.PAYOUT_RECORD_KEY``) is read. When set, it
    must equal the profile's payout address: then the payee is that address
    with source ``"ens"``; otherwise PayeeError PAYEE_MISMATCH (fail closed,
    nothing is paid to either). A failed lookup is PAYEE_UNRESOLVED. With
    ``ENS_RESOLVE_PAYEES=1``, an active ENS name and a non-empty record are
    required; missing configuration is therefore not silently redirected to
    the profile payee. With the flag off, the profile fallback remains
    available for local development and migration."""
    from app.engagements.service import payee_address
    profile = payee_address(agent)
    resolver = get_resolver()
    strict = bool(current_app.config.get("ENS_RESOLVE_PAYEES")) if has_app_context() else False
    if resolver is None:
        if strict:
            raise PayeeError("ENS payee resolution is not configured", "PAYEE_UNRESOLVED", 503)
        return Payee(profile, "profile")
    row = None
    row = (EnsName.query.filter_by(agent_id=agent.id, kind="agent", status="active")
           .order_by(EnsName.updated_at.desc()).first())
    if row is None:
        if strict:
            raise PayeeError("agent has no active ENS payout name", "PAYEE_UNRESOLVED", 503)
        return Payee(profile, "profile")
    from chain.ens_v2 import ENSResolutionError
    try:
        record = resolver.payout_address(row.name)
    except ENSResolutionError as exc:
        log.warning("names: payee lookup for %s failed: %s", row.name, exc)
        raise PayeeError(f"could not resolve the payout address of {row.name}",
                         "PAYEE_UNRESOLVED", 503) from None
    if record is None:
        if strict:
            raise PayeeError(f"{row.name} has no x402 payout record",
                             "PAYEE_UNRESOLVED", 503)
        return Payee(profile, "profile", row.name)
    if profile is None or record.lower() != profile:
        log.warning("names: %s payout record differs from the agent profile", row.name)
        raise PayeeError(f"the payout address in {row.name} does not match the agent's "
                         "profile; payment refused", "PAYEE_MISMATCH", 403)
    return Payee(profile, "ens", row.name)


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


def _listing_url(agent: Agent) -> str | None:
    """The agent's public listing page (ENSIP-26 ``web`` endpoint), when the
    app knows its public https origin."""
    base = (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/agent/{agent.id}" if base.startswith("https://") else None


def _advertised_payee(agent: Agent) -> str | None:
    """The payee an agent's name advertises: its payout address, unless payee
    screening refused that address. Pausing or unlisting the agent doesn't
    remove it, so work already under way can still be paid."""
    from app.engagements.service import payee_address
    from app.seller.stamp import onboarding_screening
    payee = payee_address(agent)
    screening = onboarding_screening(agent, payee) if payee else None
    return None if screening is not None and screening.verdict == "REFUSE" else payee


def _agent_records(agent: Agent) -> dict:
    """What the agent's name publishes. The payee unless screening refused
    it, and the stamped manifest hash only while the operator stamp is valid."""
    from app.seller.stamp import stamp_status
    manifest = _manifest(agent)
    endpoints = manifest.get("endpoints") if isinstance(manifest.get("endpoints"), dict) else {}
    stamp = stamp_status(agent)
    return _clean({
        "context": (agent.description or "")[:1000],
        "mcp": manifest.get("mcp_endpoint") or endpoints.get("mcp"),
        "a2a": endpoints.get("a2a"),
        "web": _listing_url(agent),
        "payout": _advertised_payee(agent),
        "manifest_hash": stamp.stamped_hash if stamp.ok else None,
        "erc8004_agent_id": manifest.get("erc8004_agent_id"),
    })


def _push_agent(row: EnsName) -> str | None:
    """Re-send an agent name with records recomputed now, never stale ones."""
    agent = db.session.get(Agent, row.agent_id)
    return _refresh(row, agent, force=True) if agent is not None else "NOT_FOUND: the agent is gone"


def _refresh(row: EnsName, agent: Agent, *, force: bool = False) -> str | None:
    """Push the agent's current records. If updating an active name fails,
    the row keeps the records that are on-chain, so the next publish retries."""
    previous, records = row.records, _agent_records(agent)
    row.records = records
    error = _push(row, update=force or previous != records)
    if error and row.status == "active":
        row.records = previous
    return error


def _agent_row(agent: Agent) -> EnsName:
    existing = agent_name_row(agent)
    if existing is not None:
        return existing
    root = _root_row()
    label = agent_label(agent)
    name = f"{label}.{root.name}"
    taken = db.session.get(EnsName, name)
    if taken is not None and taken.agent_id != agent.id:
        name = f"{label[:30]}-{agent.public_id.replace('-', '').lower()[3:11]}.{root.name}"
    row = _get_or_create(name, kind="agent", parent_name=root.name)
    if row.status == "revoked":
        row.status = "pending"       # re-issue; the sidecar deploys fresh proxies
    row.agent_id = agent.id
    row.records = _agent_records(agent)
    return row


def _grantee(row: EnsName) -> str | None:
    """Who may write the row's agent-editable records: the hired agent on a
    job; on an agent's own name, the payee it advertises (endpoint records
    only). None on an agent name revokes its grants."""
    from app.engagements.service import payee_address
    if row.kind == "agent":
        return (row.records or {}).get("payout")
    if row.kind == "job" and row.engagement_id:
        engagement = db.session.get(Engagement, row.engagement_id)
        agent = engagement.agent if engagement else None
    else:
        return None
    return payee_address(agent) if agent else None


def _push(row: EnsName, *, with_parent: bool = False, update: bool = False) -> str | None:
    """Send the row's intended state to the sidecar and record the outcome.
    ``with_parent`` (retry only) first re-pushes a pending/failed parent.
    ``update`` re-sends an active agent name (changed records, grants); it
    stays active if that fails, since the name itself still resolves.
    Returns why the name could not be pushed, or None."""
    client = get_client()
    label = row.name.split(".", 1)[0]
    settling = (row.records or {}).get("status") == SETTLED
    updating = update and row.status == "active" and row.kind == "agent" and not settling
    if row.status == "revoked" or (row.status == "active" and not settling and not updating):
        return None
    if settling and not row.tx_hashes:
        row.status = "revoked"      # never reached the chain; nothing to undo
        return None
    if not client.configured:
        if not updating:
            row.status = "pending"
        return "NOT_CONFIGURED: the names sidecar is not configured"
    parent = db.session.get(EnsName, row.parent_name) if row.parent_name else None
    if not settling and row.kind in ("job", "subjob"):
        if with_parent and parent is not None and parent.status in ("pending", "failed"):
            if parent.kind == "agent":
                _push_agent(parent)
            else:
                _push(parent, with_parent=True)
        if parent is None or parent.status != "active":
            row.status = "pending"
            return f"PARENT_NOT_ACTIVE: {row.parent_name} is not active yet"
    try:
        if settling:
            result = client.revoke(row.name, grantee=_grantee(row))
        elif row.kind == "agent":
            agent = db.session.get(Agent, row.agent_id)
            result = client.create_agent(agent_public_id=agent.public_id, label=label, records=row.records,
                                         grantee=_grantee(row))
        elif row.kind == "job":
            result = client.create_job(parent=row.parent_name, label=label, expiry=row.expiry,
                                       records=row.records, grantee=_grantee(row))
        elif row.kind == "subjob":
            result = client.create_subjob(parent=row.parent_name, label=label, expiry=row.expiry,
                                          records=row.records)
        else:
            return None     # the root is set up by an operator on the sidecar
    except SidecarError as exc:
        if not updating:
            row.status = "failed"
        log.warning("names: %s %s failed: %s (%s)", "revoke" if settling else "update" if updating else "issue",
                    row.name, exc, exc.code)
        return f"{exc.code}: {exc}"
    hashes = [t.get("hash") for t in result.get("txs", []) if isinstance(t, dict) and t.get("hash")]
    row.tx_hashes = list(dict.fromkeys([*(row.tx_hashes or []), *hashes]))
    row.owner = result.get("owner") or row.owner
    if result.get("expiry"):
        row.expiry = int(result["expiry"])
    if settling:
        row.status = "revoked"
        for child in EnsName.query.filter_by(parent_name=row.name).all():
            child.status = "revoked"
        return None
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
    return None


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


def resolve_name(name: str) -> dict | None:
    """Return the application's latest active ENS record for a named agent.

    The payment route still performs its own fresh Universal Resolver check
    when strict ENS payee resolution is enabled. This read endpoint is for
    named-agent discovery and never supplies a treasury or profile fallback.
    """
    wanted = str(name or "").strip().rstrip(".").lower()
    row = db.session.get(EnsName, wanted)
    if row is None or row.status != "active" or row.kind != "agent":
        return None
    return _row_dict(row)


def _row_dict(r: EnsName) -> dict:
    return {
        "name": r.name, "node": r.node, "kind": r.kind, "status": r.status,
        "parent_name": r.parent_name, "engagement_id": r.engagement_id, "agent_id": r.agent_id,
        "owner": r.owner, "expiry": r.expiry,
        "expires_at": datetime.fromtimestamp(r.expiry, timezone.utc).isoformat() if r.expiry else None, "records": r.records or {}, "tx_hashes": r.tx_hashes or [],
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
