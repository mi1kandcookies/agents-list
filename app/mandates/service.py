"""Mandate issuance, attenuation, spending and revocation
(docs/decisions/0001-custody-chain.md §5).

A root mandate is issued to the hired agent from one consumed
``engagement.fund`` approval. Every sub-hire gets a child mandate whose caps
can only narrow. A child's budget is reserved out of its parent: the parent's
remaining allowance is ``budget − spent − reserved``, where each active child
reserves its full budget and each revoked or expired child keeps only what it
already spent.

Error codes (``MandateError.code``):

    MANDATE_INVALID       bad signature/header, unknown mandate, token ≠ stored row,
                          bad inputs, root approval not consumed / wrong kind / wrong engagement
    MANDATE_EXCEEDED      budget, per-transaction cap, expiry or payees wider than allowed
    MANDATE_EXPIRED       the mandate or an ancestor is past ``exp``
    MANDATE_REVOKED       the mandate or an ancestor is revoked
    DEPTH_EXCEEDED        deeper than the root's ``max_depth`` (or ``MANDATE_MAX_DEPTH``)
    CATEGORY_NOT_ALLOWED  categories not a subset of the parent's
    BANNED                the approving human is banned
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.common.ids import new_id
from app.extensions import db
from app.mandates import tokens
from app.mandates.tokens import MandateError, check_attenuation, hum_hash, normalize_categories

__all__ = ["MandateError", "issue_root", "attenuate", "verify_chain", "spend", "revoke",
           "chain_graph", "status_of", "max_depth_limit"]

ROOT_APPROVAL_KIND = "engagement.fund"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _ts(value) -> int:
    """Unix seconds from an int or a datetime (naive datetimes are UTC)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, bool) or not isinstance(value, int):
        raise MandateError("MANDATE_INVALID", "exp must be unix seconds or a datetime")
    return value


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _micro(value, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MandateError("MANDATE_INVALID", f"{name} must be an integer (micro-USDC)")
    if value < 0 or (value == 0 and not allow_zero):
        raise MandateError("MANDATE_INVALID", f"{name} must be positive")
    return value


def max_depth_limit() -> int:
    from flask import current_app, has_app_context
    raw = current_app.config.get("MANDATE_MAX_DEPTH") if has_app_context() else None
    if raw is None:
        raw = os.environ.get("MANDATE_MAX_DEPTH", "2")
    return int(raw)


def _models():
    from app.models import Approval, Engagement, Human, Mandate
    return Approval, Engagement, Human, Mandate


def _get(mandate_id: str):
    Mandate = _models()[3]
    row = db.session.get(Mandate, mandate_id)
    if row is None:
        raise MandateError("MANDATE_INVALID", f"unknown mandate {mandate_id}")
    return row


def _lock(mandate_id: str):
    """Take a write lock on one mandate row and return it freshly loaded.

    The no-op UPDATE takes the row lock on Postgres and the database write
    lock on SQLite (where ``FOR UPDATE`` is ignored), so concurrent spends and
    attenuations against the same parent serialize.
    """
    Mandate = _models()[3]
    db.session.execute(update(Mandate).where(Mandate.id == mandate_id)
                       .values(spent_micro=Mandate.spent_micro))
    row = db.session.execute(
        select(Mandate).where(Mandate.id == mandate_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        raise MandateError("MANDATE_INVALID", f"unknown mandate {mandate_id}")
    return row


def _reserved_by_children(row, now: int) -> int:
    """What ``row``'s children hold against its budget."""
    Mandate = _models()[3]
    total = 0
    for child in db.session.execute(select(Mandate).where(Mandate.parent_id == row.id)).scalars():
        active = child.revoked_at is None and _ts(child.expires_at) > now
        total += child.budget_micro if active else child.spent_micro
    return total


def _available(row, now: int) -> int:
    return row.budget_micro - row.spent_micro - _reserved_by_children(row, now)


def _ancestry(row) -> list:
    """``[row, parent, …, root]``, loaded from the database."""
    chain = [row]
    seen = {row.id}
    while chain[-1].parent_id is not None:
        parent = _get(chain[-1].parent_id)
        if parent.id in seen:
            raise MandateError("MANDATE_INVALID", "mandate chain has a cycle")
        seen.add(parent.id)
        chain.append(parent)
    if chain[-1].id != row.root_id:
        raise MandateError("MANDATE_INVALID", "mandate chain does not end at its root")
    return chain


def _check_human(human) -> None:
    if human is None:
        raise MandateError("MANDATE_INVALID", "mandate has no human")
    if human.banned:
        raise MandateError("BANNED", "the approving human is banned")


def _check_root_approval(approval, engagement) -> None:
    if approval is None or approval.kind != ROOT_APPROVAL_KIND:
        raise MandateError("MANDATE_INVALID", f"root approval must be of kind {ROOT_APPROVAL_KIND}")
    if approval.state != "consumed" or approval.consumed_at is None:
        raise MandateError("MANDATE_INVALID", "root approval has not been consumed")
    if approval.engagement_id != engagement.id:
        raise MandateError("MANDATE_INVALID", "root approval is for another engagement")


def _check_chain(row, now: int | None = None) -> list:
    """Re-walk ``row``'s ancestry and re-check every §5 rule. Returns the
    ancestry ``[row, …, root]``."""
    Approval, Engagement, Human, _ = _models()
    now = _now() if now is None else now
    chain = _ancestry(row)
    root = chain[-1]
    for node in chain:
        if node.revoked_at is not None:
            raise MandateError("MANDATE_REVOKED", f"mandate {node.id} is revoked")
    for node in chain:
        if _ts(node.expires_at) <= now:
            raise MandateError("MANDATE_EXPIRED", f"mandate {node.id} has expired")
    _check_human(db.session.get(Human, root.human_id))
    engagement = db.session.get(Engagement, root.engagement_id)
    if engagement is None:
        raise MandateError("MANDATE_INVALID", "root engagement not found")
    _check_root_approval(db.session.get(Approval, root.approval_id) if root.approval_id else None,
                         engagement)
    if root.depth != 0 or root.max_depth > max_depth_limit():
        raise MandateError("DEPTH_EXCEEDED", "root mandate depth is out of range")
    for child, parent in zip(chain, chain[1:]):
        if child.depth != parent.depth + 1 or child.depth > root.max_depth:
            raise MandateError("DEPTH_EXCEEDED", f"mandate {child.id} is too deep")
        if child.human_id != root.human_id or child.root_id != root.id:
            raise MandateError("MANDATE_INVALID", f"mandate {child.id} does not belong to its root")
        check_attenuation(_cap(parent), _ts(parent.expires_at), _cap(child), _ts(child.expires_at))
    root_claims = tokens.decode(root.token, verify_exp=False)
    for node in chain[:-1]:
        claims = tokens.decode(node.token, verify_exp=False)
        if claims.get("apr") != root.approval_id or claims.get("hum") != root_claims.get("hum"):
            raise MandateError("MANDATE_INVALID", f"mandate {node.id} does not trace to its root approval")
    return chain


def _cap(row) -> dict:
    """The row's caps, with ``per_tx_max_micro``/``payees`` read from its
    (already verified or self-issued) token, since the table has no column
    for them."""
    claims = tokens.decode(row.token, verify_exp=False)
    cap = claims["cap"]
    recorded = {"jti": row.id, "sub": row.grantee_agent_public_id, "root": row.root_id,
                "par": row.parent_id, "dep": row.depth, "eng": row.engagement_id,
                "apr": row.approval_id if row.parent_id is None else claims.get("apr"),
                "exp": _ts(row.expires_at)}
    if (any(claims.get(k) != v for k, v in recorded.items())
            or cap.get("budget_micro") != row.budget_micro
            or cap.get("max_depth") != row.max_depth
            or list(cap.get("categories") or []) != list(row.categories or [])):
        raise MandateError("MANDATE_INVALID", f"mandate {row.id} token does not match its record")
    return {"budget_micro": row.budget_micro, "categories": list(row.categories or []),
            "max_depth": row.max_depth, "per_tx_max_micro": int(cap["per_tx_max_micro"]),
            "payees": cap.get("payees")}


def _claims(*, mandate_id, grantee, hum, root_id, parent_id, depth, approval_id, engagement,
            cap, now, exp) -> dict:
    return {"iss": tokens.ISSUER, "jti": mandate_id, "sub": grantee, "hum": hum,
            "root": root_id, "par": parent_id, "dep": depth, "apr": approval_id,
            "eng": engagement.id, "sow": engagement.sow_hash, "cap": cap,
            "iat": now, "nbf": now, "exp": exp}


def _grantee(agent_public_id) -> str:
    from app.common.agent_ids import AgentIdError, normalize
    try:
        return normalize(str(agent_public_id))
    except (AgentIdError, TypeError, ValueError) as exc:
        raise MandateError("MANDATE_INVALID", f"invalid agent id {agent_public_id!r}") from exc


# ── Public API ───────────────────────────────────────────────────────────────

def issue_root(engagement, human, approval, grantee_agent_public_id: str, budget_micro: int,
               categories, max_depth: int, per_tx_max_micro: int, exp):
    """Issue the root mandate for ``engagement`` from a consumed
    ``engagement.fund`` ``approval`` by ``human``. Returns the ``Mandate``
    row (committed); its ``token`` is the signed JWS."""
    Mandate = _models()[3]
    now = _now()
    _check_human(human)
    _check_root_approval(approval, engagement)
    if approval.human_sub and approval.human_sub != human.world_sub:
        raise MandateError("MANDATE_INVALID", "root approval was given by another human")
    if engagement.buyer_human_id is not None and engagement.buyer_human_id != human.id:
        raise MandateError("MANDATE_INVALID", "human is not this engagement's buyer")
    budget = _micro(budget_micro, "budget_micro")
    per_tx = _micro(per_tx_max_micro, "per_tx_max_micro")
    ceiling = approval.action.get("amount_micro", engagement.total_micro)
    if budget > ceiling:
        raise MandateError("MANDATE_EXCEEDED", "root budget exceeds the approved amount")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise MandateError("MANDATE_INVALID", "max_depth must be a non-negative integer")
    if max_depth > max_depth_limit():
        raise MandateError("DEPTH_EXCEEDED", f"max_depth exceeds MANDATE_MAX_DEPTH={max_depth_limit()}")
    cats = normalize_categories(categories)
    if not cats:
        raise MandateError("MANDATE_INVALID", "a mandate needs at least one category")
    exp_ts = _ts(exp)
    if exp_ts <= now:
        raise MandateError("MANDATE_EXPIRED", "mandate expiry must be in the future")
    grantee = _grantee(grantee_agent_public_id)

    mandate_id = new_id("MND")
    cap = {"budget_micro": budget, "categories": cats, "max_depth": max_depth,
           "per_tx_max_micro": per_tx, "payees": None}
    token = tokens.encode(_claims(mandate_id=mandate_id, grantee=grantee, hum=hum_hash(human.world_sub),
                                  root_id=mandate_id, parent_id=None, depth=0,
                                  approval_id=approval.id, engagement=engagement,
                                  cap=cap, now=now, exp=exp_ts))
    row = Mandate(id=mandate_id, parent_id=None, root_id=mandate_id, engagement_id=engagement.id,
                  human_id=human.id, grantee_agent_public_id=grantee, budget_micro=budget,
                  spent_micro=0, categories=cats, max_depth=max_depth, depth=0,
                  expires_at=_dt(exp_ts), token=token, approval_id=approval.id)
    db.session.add(row)
    db.session.commit()
    return row


def attenuate(parent_token: str, child_agent_public_id: str, budget_micro: int, categories, exp,
              per_tx_max_micro: int | None = None):
    """Issue a narrower child mandate under ``parent_token`` for a sub-hire.
    ``per_tx_max_micro`` defaults to the parent's cap (bounded by the child's
    budget). Returns the committed child ``Mandate`` row."""
    Mandate = _models()[3]
    now = _now()
    parent_claims = verify_chain(parent_token)
    budget = _micro(budget_micro, "budget_micro")
    cats = normalize_categories(categories)
    if not cats:
        raise MandateError("MANDATE_INVALID", "a mandate needs at least one category")
    exp_ts = _ts(exp)
    if exp_ts <= now:
        raise MandateError("MANDATE_EXPIRED", "mandate expiry must be in the future")
    grantee = _grantee(child_agent_public_id)

    try:
        parent = _lock(parent_claims["jti"])
        chain = _check_chain(parent, now)
        root = chain[-1]
        parent_cap = _cap(parent)
        per_tx = (min(parent_cap["per_tx_max_micro"], budget) if per_tx_max_micro is None
                  else _micro(per_tx_max_micro, "per_tx_max_micro"))
        depth = parent.depth + 1
        if depth > root.max_depth or depth > max_depth_limit():
            raise MandateError("DEPTH_EXCEEDED",
                               f"depth {depth} exceeds the root's max_depth {root.max_depth}")
        cap = {"budget_micro": budget, "categories": cats, "max_depth": root.max_depth,
               "per_tx_max_micro": per_tx, "payees": parent_cap["payees"]}
        check_attenuation(parent_cap, _ts(parent.expires_at), cap, exp_ts)
        available = _available(parent, now)
        if budget > available:
            raise MandateError("MANDATE_EXCEEDED",
                               f"child budget {budget} exceeds the parent's remaining {available}")

        mandate_id = new_id("MND")
        token = tokens.encode(_claims(mandate_id=mandate_id, grantee=grantee,
                                      hum=parent_claims["hum"], root_id=root.id,
                                      parent_id=parent.id, depth=depth,
                                      approval_id=root.approval_id,
                                      engagement=_engagement(parent.engagement_id),
                                      cap=cap, now=now, exp=exp_ts))
        child = Mandate(id=mandate_id, parent_id=parent.id, root_id=root.id,
                        engagement_id=parent.engagement_id, human_id=root.human_id,
                        grantee_agent_public_id=grantee, budget_micro=budget, spent_micro=0,
                        categories=cats, max_depth=root.max_depth, depth=depth,
                        expires_at=_dt(exp_ts), token=token, approval_id=None)
        db.session.add(child)
        db.session.commit()
        return child
    except BaseException:
        db.session.rollback()
        raise


def _engagement(engagement_id: str):
    Engagement = _models()[1]
    eng = db.session.get(Engagement, engagement_id)
    if eng is None:
        raise MandateError("MANDATE_INVALID", "engagement not found")
    return eng


def verify_chain(token: str) -> dict:
    """Verify ``token``'s signature and that it is the stored token of a live
    mandate, then re-walk and re-check its whole chain in the database.
    Returns the verified claims."""
    claims = tokens.decode(token)
    Mandate = _models()[3]
    row = db.session.get(Mandate, claims["jti"])
    if row is None or row.token != token:
        raise MandateError("MANDATE_INVALID", "mandate token is not on record")
    _check_chain(row)
    return claims


def spend(mandate_id: str, amount_micro: int, *, category: str | None = None):
    """Record ``amount_micro`` spent under ``mandate_id`` after re-checking its
    chain, per-transaction cap and remaining budget under a row lock.
    Returns the updated ``Mandate`` row."""
    amount = _micro(amount_micro, "amount_micro")
    now = _now()
    try:
        row = _lock(mandate_id)
        _check_chain(row, now)
        cap = _cap(row)
        if category is not None and category not in cap["categories"]:
            raise MandateError("CATEGORY_NOT_ALLOWED", f"category {category!r} is not in the mandate")
        if amount > cap["per_tx_max_micro"]:
            raise MandateError("MANDATE_EXCEEDED", "amount exceeds the per-transaction cap")
        available = _available(row, now)
        if amount > available:
            raise MandateError("MANDATE_EXCEEDED",
                               f"amount {amount} exceeds the remaining budget {available}")
        row.spent_micro += amount
        db.session.commit()
        return row
    except BaseException:
        db.session.rollback()
        raise


def revoke(mandate_id: str) -> list[str]:
    """Revoke a mandate and every descendant. Returns the ids newly revoked
    (already-revoked mandates keep their original ``revoked_at``)."""
    Mandate = _models()[3]
    _get(mandate_id)
    at = datetime.now(timezone.utc)
    revoked: list[str] = []
    frontier = [mandate_id]
    seen: set[str] = set()
    while frontier:
        batch = [m for m in frontier if m not in seen]
        seen.update(batch)
        rows = db.session.execute(select(Mandate).where(Mandate.id.in_(batch))).scalars().all()
        for row in rows:
            if row.revoked_at is None:
                row.revoked_at = at
                revoked.append(row.id)
        frontier = list(db.session.execute(
            select(Mandate.id).where(Mandate.parent_id.in_(batch))).scalars())
    db.session.commit()
    return revoked


def status_of(row, now: int | None = None) -> str:
    """``revoked`` | ``expired`` | ``exhausted`` | ``active`` for this row alone
    (use ``verify_chain`` for the full chain verdict)."""
    now = _now() if now is None else now
    if row.revoked_at is not None:
        return "revoked"
    if _ts(row.expires_at) <= now:
        return "expired"
    if _available(row, now) <= 0:
        return "exhausted"
    return "active"


def chain_graph(engagement_id: str) -> dict:
    """``{nodes, edges}`` for every mandate tree touching ``engagement_id``.

    Nodes: ``human`` (id ``HUM-`` + hashed sub prefix, ``hum`` = full sha256;
    never the raw sub), ``mandate`` (budget/spent/categories/depth/exp/status)
    and ``agent`` (grantee public ids). Edges: human→root (``approved``),
    parent→child mandate (``attenuates``), mandate→agent (``grants``).
    """
    _, _, Human, Mandate = _models()
    now = _now()
    root_ids = set(db.session.execute(
        select(Mandate.root_id).where(Mandate.engagement_id == engagement_id)).scalars())
    rows = db.session.execute(
        select(Mandate).where(Mandate.root_id.in_(root_ids))
        .order_by(Mandate.depth, Mandate.id)).scalars().all() if root_ids else []

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add(node: dict) -> None:
        if node["id"] not in seen:
            seen.add(node["id"])
            nodes.append(node)

    agent_names = _agent_names({r.grantee_agent_public_id for r in rows})
    for row in rows:
        if row.parent_id is None:
            human = db.session.get(Human, row.human_id)
            hum = hum_hash(human.world_sub) if human else None
            human_node = f"HUM-{hum[:12]}" if hum else f"HUM-unknown-{row.human_id}"
            add({"id": human_node, "type": "human", "hum": hum,
                 "banned": bool(human and human.banned)})
            edges.append({"from": human_node, "to": row.id, "kind": "approved",
                          "approval_id": row.approval_id})
        else:
            edges.append({"from": row.parent_id, "to": row.id, "kind": "attenuates"})
        add({"id": row.id, "type": "mandate", "engagement_id": row.engagement_id,
             "parent_id": row.parent_id, "root_id": row.root_id,
             "grantee": row.grantee_agent_public_id, "budget_micro": row.budget_micro,
             "spent_micro": row.spent_micro, "categories": list(row.categories or []),
             "depth": row.depth, "max_depth": row.max_depth, "exp": _ts(row.expires_at),
             "status": status_of(row, now)})
        add({"id": row.grantee_agent_public_id, "type": "agent",
             "name": agent_names.get(row.grantee_agent_public_id)})
        edges.append({"from": row.id, "to": row.grantee_agent_public_id, "kind": "grants"})
    return {"engagement_id": engagement_id, "nodes": nodes, "edges": edges}


def _agent_names(public_ids: set[str]) -> dict[str, str]:
    if not public_ids:
        return {}
    from app.models import Agent
    return dict(db.session.execute(
        select(Agent.public_id, Agent.name).where(Agent.public_id.in_(public_ids))).all())


def remaining_micro(row) -> int:
    return max(0, _available(row, _now()))


def reserved_micro(row) -> int:
    return _reserved_by_children(row, _now())

