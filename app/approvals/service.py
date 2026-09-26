"""Approval lifecycle (docs/decisions/0001-custody-chain.md §3).

One approval = one fresh World ID authentication bound to one action hash::

    created ─start─▶ pending ─valid id_token─▶ approved ─consume+execute─▶ consumed
                       ├─ access_denied ─▶ denied
                       ├─ past expires_at / expired_token ─▶ expired
                       ├─ cancel ─▶ cancelled
                       └─ validation failure ─▶ rejected (failure_code)
    created ─ screening REFUSE / banned / cap ─▶ blocked (never sent to the provider)
    approved ─ executor error ─▶ failed

Binding: the web flow sends ``action_nonce(action)`` as the OIDC nonce and the
ID token must echo it. The device flow sends it too, and the device_code is
stored on this approval row; if the provider's token omits the nonce, that
server-side binding stands and a ``nonce_absent`` event is written. A nonce
that is present but different is always rejected.

Every accepted ID token's ``jti`` goes into ``used_id_token_jtis``; a repeat
rejects the approval with REPLAYED_TOKEN. ``auth_time`` must be no earlier
than the approval's creation and at most STEPUP_MAX_AGE_SECONDS old.

``consume()`` re-selects the row ``FOR UPDATE`` and re-checks state, expiry,
action hash, ban, weekly cap, the engagement's claimed human, the kind's
``before_consume`` checks (e.g. the payee agent is still hireable) and screening
(a fresh screen of the payee for money-moving kinds) in the same transaction
that runs the executor, so an approval executes at most
once. When a callback or poll reaches ``approved``, the executor runs at once.
After a successful execution commits, the kind's after-consume hooks run
(``executors.after_consume``); they never undo or fail the approval. When an
approval ends denied / expired / cancelled / rejected / blocked, the kind's
``executors.on_terminal`` hooks run in the same transaction (e.g. to return
the engagement it would have funded to a retryable state).

Every state change writes an ``approval_events`` row.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from flask import current_app, has_app_context
from sqlalchemy.exc import IntegrityError

from app.approvals import actions
from app.approvals.errors import ApprovalError, ApprovalNotFound, ApprovalStateError
from app.approvals.executors import (EXECUTORS, UNEXECUTED_TERMINAL, ExecutionResult,
                                     run_after_consume, run_before_consume, run_on_terminal)
from app.common.ids import new_id
from app.extensions import db
from app.humans import service as humans
from app.identity import pkce
from app.identity.world import IdTokenError, WorldClient, WorldError
from app.models import (Approval, ApprovalEvent, Engagement, Human, Screening,
                        UsedIdTokenJti)

log = logging.getLogger("agents_list.approvals")

__all__ = [
    "ApprovalError", "ApprovalNotFound", "ApprovalStateError", "ExecutionResult",
    "create_approval", "start_web", "complete_web", "start_device", "poll", "cancel",
    "consume", "get", "to_dict", "world_client",
]

DEFAULT_TTL_SECONDS = 180
DEFAULT_STEPUP_MAX_AGE_SECONDS = 300
FLOWS = ("web", "device")

TERMINAL = frozenset({"consumed", "denied", "expired", "cancelled", "rejected", "blocked", "failed"})
TRANSITIONS = {
    "created":  {"pending", "cancelled", "expired", "blocked"},
    "pending":  {"approved", "denied", "expired", "cancelled", "rejected"},
    "approved": {"consumed", "failed", "expired", "blocked"},
}

# Failure codes beyond IdTokenError / ApprovalError codes.
REPLAYED_TOKEN = "REPLAYED_TOKEN"
IDP_ERROR = "IDP_ERROR"
EXECUTOR_ERROR = "EXECUTOR_ERROR"

# Overridable in tests; the World client keeps its own clock.
clock = time.time


# ── helpers ─────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.fromtimestamp(clock(), tz=timezone.utc)


def _aware(value: datetime) -> datetime:
    """Datetimes come back naive from SQLite; every stored value is UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def unix(value: datetime) -> int:
    return int(_aware(value).timestamp())


def _setting(name: str, default: int) -> int:
    value = None
    if has_app_context():
        value = current_app.config.get(name)
    if value in (None, ""):
        value = os.environ.get(name, "").strip() or default
    return int(value)


def ttl_seconds() -> int:
    return _setting("APPROVAL_TTL_SECONDS", DEFAULT_TTL_SECONDS)


def stepup_max_age_seconds() -> int:
    return _setting("STEPUP_MAX_AGE_SECONDS", DEFAULT_STEPUP_MAX_AGE_SECONDS)


def world_client() -> WorldClient:
    """One WorldClient per app (it caches discovery and JWKS)."""
    client = current_app.extensions.get("world_client")
    if client is None:
        client = current_app.extensions["world_client"] = WorldClient()
    return client


def _event(approval: Approval, event: str, **detail) -> None:
    db.session.add(ApprovalEvent(approval_id=approval.id, event=event, detail=detail or None,
                                 at=_now()))


def _transition(approval: Approval, state: str, *, code: Optional[str] = None,
                detail: Optional[str] = None, **event_detail) -> None:
    current = approval.state
    if state not in TRANSITIONS.get(current, ()):
        raise ApprovalStateError(current, f"invalid approval transition {current} -> {state}")
    approval.state = state
    if code:
        approval.failure_code = code
        approval.failure_detail = (detail or "")[:2000] or None
        event_detail["code"] = code
        if detail:
            event_detail["detail"] = detail[:500]
    _event(approval, state, **{"from": current, **event_detail})
    log.info("approval %s %s -> %s%s", approval.id, current, state, f" ({code})" if code else "")
    if state in UNEXECUTED_TERMINAL:
        run_on_terminal(approval, approval.action)


def _lock(approval_id: str) -> Approval:
    """Re-select the row, taking a row lock where the database supports it."""
    row = db.session.get(Approval, approval_id, with_for_update=True, populate_existing=True)
    if row is None:
        raise ApprovalNotFound(approval_id)
    return row


def _expire_if_due(approval: Approval) -> bool:
    if approval.state in TERMINAL or _now() <= _aware(approval.expires_at):
        return False
    _transition(approval, "expired", reason="ttl")
    return True


def get(approval_id: str) -> Approval:
    row = db.session.get(Approval, approval_id)
    if row is None:
        raise ApprovalNotFound(approval_id)
    return row


# ── create ──────────────────────────────────────────────────────────────────

def create_approval(kind: str, action: dict, *, flow: str, engagement_id: Optional[str] = None,
                    milestone_id: Optional[int] = None, agent_id: Optional[int] = None,
                    screening_id: Optional[str] = None) -> Approval:
    """Create an approval for ``action``.

    ``action`` is either a full v1 action (with ``approval_id`` and ``exp``,
    from ``actions.build_action``) or just its kind-specific fields, in which
    case the id and ``exp = now + APPROVAL_TTL_SECONDS`` are filled in here.

    Returns the committed approval: ``created``, or ``blocked`` (never sent to
    the provider) when the screening verdict is REFUSE or the engagement's
    claimed human is banned or over the weekly cap.
    """
    if flow not in FLOWS:
        raise ValueError(f"flow must be one of {FLOWS}")
    if "approval_id" in action:
        if action.get("kind") != kind:
            raise ValueError(f"action kind {action.get('kind')!r} != {kind!r}")
        fields = {k: v for k, v in action.items() if k not in ("v", "kind")}
    else:
        fields = dict(action)
        fields["approval_id"] = new_id("APR")
        fields["exp"] = int(_now().timestamp()) + ttl_seconds()
    action = actions.build_action(kind, **fields)
    screening_id = screening_id or action.get("screening_id")
    engagement_id = engagement_id or action.get("engagement_id")

    approval = Approval(
        id=action["approval_id"], kind=kind,
        action_json=actions.canonical(action).decode("utf-8"),
        action_hash=actions.action_hash(action), nonce=actions.action_nonce(action),
        flow=flow, state="created",
        expires_at=datetime.fromtimestamp(action["exp"], tz=timezone.utc),
        engagement_id=engagement_id, milestone_id=milestone_id, agent_id=agent_id,
        screening_id=screening_id, created_at=_now(),
    )
    db.session.add(approval)
    _event(approval, "created", kind=kind, flow=flow, action_hash=approval.action_hash)

    try:
        _check_screening(approval)
        buyer = _engagement_buyer(approval)
        if buyer is not None:
            humans.ensure_not_banned(buyer)
            if kind in humans.CAPPED_KINDS:
                humans.check_weekly_cap(buyer, int(action.get("amount_micro") or 0), now=_now())
    except ApprovalError as exc:
        _transition(approval, "blocked", code=exc.code, detail=exc.message)
    db.session.commit()
    return approval


def _check_screening(approval: Approval) -> None:
    """The screening bound at create time must exist and not be REFUSE."""
    if not approval.screening_id:
        return
    screening = db.session.get(Screening, approval.screening_id)
    if screening is None or screening.verdict == "REFUSE":
        raise ApprovalError("SCREENING_REFUSED",
                            "screening missing" if screening is None else "screening verdict REFUSE")


# Money-moving kinds and the screening hop they are re-screened as in consume().
RESCREEN_HOPS = {"engagement.fund": "engagement.fund", "milestone.release": "milestone.release",
                 "subhire.fund": "subhire.hop"}


def _rescreen(approval: Approval, action: dict) -> None:
    """Fresh screen of both payer and payee before money moves.

    Uses the same policy as payment hops: REFUSE, an expired verdict, an
    amount over a CAP, or an unacknowledged ASK_HUMAN all refuse. Actions
    without a payee address have nothing to screen here. The payer is the
    parent agent for a sub-hire, or the World-bound ``buyer_address`` for a
    fund/release action; an unbound buyer is left for the identity boundary to
    reject before it authorizes a production payment.
    """
    hop = RESCREEN_HOPS.get(approval.kind)
    payee = action.get("payee_address")
    if hop is None or not payee:
        return
    from app.screening import policy
    from app.engagements.service import payee_address, screen as engagement_screen
    from app.screening.service import screen
    amount = int(action.get("amount_micro") or 0)

    eng = db.session.get(Engagement, approval.engagement_id) if approval.engagement_id else None
    payer_address = None
    payer_agent = None
    payer_required = False
    if approval.kind == "subhire.fund" and eng is not None and eng.parent is not None:
        payer_agent = eng.parent.agent
        payer_address = payee_address(payer_agent) if payer_agent is not None else ""
        payer_required = True
    elif eng is not None:
        payer_address = eng.buyer_address
        payer_required = bool(payer_address) or bool(action.get("payer_screening_id"))
    if payer_required:
        payer = engagement_screen("payer.check", chain_address=payer_address or "",
                                  amount_micro=amount, engagement=eng, agent=payer_agent)
        _event(approval, "payer_rescreened", screening_id=payer.get("id"),
               verdict=payer.get("verdict"))
        try:
            policy.enforce_verdict(payer, amount,
                                   acknowledged=bool(action.get("screening_ack")))
        except policy.ScreeningBlocked as exc:
            raise ApprovalError("SCREENING_REFUSED", f"payer re-screen {exc.code}: {exc}") from None

    verdict = screen(hop, chain_address=payee, amount_micro=amount,
                     engagement_id=approval.engagement_id, agent_id=approval.agent_id)
    _event(approval, "rescreened", screening_id=verdict.get("id"), verdict=verdict.get("verdict"))
    try:
        policy.enforce_verdict(verdict, amount, acknowledged=bool(action.get("screening_ack")))
    except policy.ScreeningBlocked as exc:
        raise ApprovalError("SCREENING_REFUSED", f"re-screen {exc.code}: {exc}") from None


def _engagement_buyer(approval: Approval) -> Optional[Human]:
    if not approval.engagement_id:
        return None
    eng = db.session.get(Engagement, approval.engagement_id)
    if eng is None or eng.buyer_human_id is None:
        return None
    return db.session.get(Human, eng.buyer_human_id)


# ── web flow ────────────────────────────────────────────────────────────────

def start_web(approval: Approval) -> str:
    """Begin (or restart) the browser flow; returns the authorization URL.

    Sends prompt=login, max_age=0, nonce=action_nonce(action), a fresh state
    and a PKCE S256 challenge. Restarting a pending web approval replaces its
    state, so an older callback no longer matches.
    """
    approval = _lock(approval.id)
    if approval.flow != "web":
        raise ApprovalStateError(approval.state, "not a web-flow approval")
    if _expire_if_due(approval):
        db.session.commit()
        raise ApprovalStateError(approval.state, "approval has expired")
    if approval.state not in ("created", "pending"):
        raise ApprovalStateError(approval.state, f"approval is {approval.state}")
    verifier = pkce.new_verifier()
    state = pkce.new_state()
    url = world_client().authorize_url(state=state, nonce=approval.nonce,
                                       code_challenge=pkce.challenge_s256(verifier),
                                       prompt="login", max_age=0)
    approval.state_param = state
    approval.pkce_verifier = verifier
    if approval.state == "created":
        _transition(approval, "pending", flow="web")
    else:
        _event(approval, "restarted", flow="web")
    db.session.commit()
    return url


def complete_web(state: str, code: Optional[str], error: Optional[str]) -> Approval:
    """Handle the provider's redirect back to /auth/world/callback."""
    if not state:
        raise ApprovalNotFound("missing state")
    found = db.session.query(Approval).filter_by(state_param=state).one_or_none()
    if found is None:
        raise ApprovalNotFound("unknown state")
    approval = _lock(found.id)
    if approval.state_param != state:  # restarted while we waited for the lock
        raise ApprovalNotFound("stale state")
    verifier = approval.pkce_verifier
    approval.state_param = None  # one callback per state
    approval.pkce_verifier = None

    if approval.state != "pending":
        _event(approval, "late_callback", state=approval.state)
        db.session.commit()
        return approval
    if _expire_if_due(approval):
        db.session.commit()
        return approval

    if error:
        if error == "access_denied":
            _transition(approval, "denied", error=error)
        else:
            _transition(approval, "rejected", code=IDP_ERROR, detail=error)
        db.session.commit()
        return approval
    if not code:
        _transition(approval, "rejected", code=IDP_ERROR, detail="callback without code")
        db.session.commit()
        return approval

    client = world_client()
    try:
        tokens = client.exchange_code(code, verifier)
        claims = client.validate_id_token(tokens["id_token"], expected_nonce=approval.nonce,
                                          not_before=unix(approval.created_at),
                                          max_age_s=stepup_max_age_seconds())
    except IdTokenError as exc:
        _transition(approval, "rejected", code=exc.code, detail=exc.message)
        db.session.commit()
        return approval
    except WorldError as exc:
        _transition(approval, "rejected", code=IDP_ERROR, detail=str(exc))
        db.session.commit()
        return approval
    return _accept(approval, claims, tokens["id_token"])


# ── device flow ─────────────────────────────────────────────────────────────

def start_device(approval: Approval) -> Approval:
    """Start an RFC 8628 device authorization for this approval.

    The nonce is sent, and the returned device_code is stored on this row:
    that row is the server-side binding between the device_code and the
    action hash. Provider errors raise WorldError and leave the approval
    ``created`` so it can be started again.
    """
    approval = _lock(approval.id)
    if approval.flow != "device":
        raise ApprovalStateError(approval.state, "not a device-flow approval")
    if _expire_if_due(approval):
        db.session.commit()
        raise ApprovalStateError(approval.state, "approval has expired")
    if approval.state == "pending":
        db.session.commit()  # release the row lock
        return approval  # already started: idempotent
    if approval.state != "created":
        raise ApprovalStateError(approval.state, f"approval is {approval.state}")
    try:
        start = world_client().device_authorize(nonce=approval.nonce)
    except WorldError as exc:
        _event(approval, "start_failed", error=str(exc)[:500])
        db.session.commit()
        raise
    approval.device_code = start.device_code
    approval.user_code = start.user_code
    approval.verification_uri = start.verification_uri
    approval.verification_uri_complete = start.verification_uri_complete
    approval.poll_interval = start.interval
    approval.next_poll_at = _now()
    _transition(approval, "pending", flow="device", nonce_sent=True,
                user_code=start.user_code, provider_expires_in=start.expires_in)
    db.session.commit()
    return approval


def poll(approval: Approval) -> Approval:
    """Advance a pending approval: expire it when due, and for the device flow
    ask the provider once (never faster than its poll interval)."""
    approval = _lock(approval.id)
    _expire_if_due(approval)
    now = _now()
    if approval.state != "pending" or approval.flow != "device" or not approval.device_code \
            or (approval.next_poll_at is not None and now < _aware(approval.next_poll_at)):
        db.session.commit()  # persist a TTL expiry, release the row lock
        return approval

    client = world_client()
    result = client.poll_device(approval.device_code)
    interval = result.interval or approval.poll_interval or 5
    if result.status in ("pending", "slow_down"):
        if result.status == "slow_down":
            _event(approval, "slow_down", interval=interval)
        approval.poll_interval = interval
        approval.next_poll_at = now + timedelta(seconds=interval)
        db.session.commit()
        return approval
    if result.status == "denied":
        _transition(approval, "denied", error=result.error)
    elif result.status == "expired":
        _transition(approval, "expired", reason="expired_token")
    elif result.status == "error":
        if result.interval is not None:
            # Transport failure (WorldClient keeps the interval): try again later.
            _event(approval, "poll_error", error=(result.error or "")[:500])
            approval.next_poll_at = now + timedelta(seconds=interval)
        else:
            _transition(approval, "rejected", code=IDP_ERROR, detail=result.error)
    else:  # approved
        return _accept_device_token(approval, result.id_token)
    db.session.commit()
    return approval


def _accept_device_token(approval: Approval, id_token: str) -> Approval:
    try:
        unverified = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        unverified = {}
    # A nonce that is present must match; an absent one falls back to the
    # device_code binding on this row (docs/decisions/0001-custody-chain.md §2).
    nonce_present = unverified.get("nonce") is not None
    try:
        claims = world_client().validate_id_token(
            id_token, expected_nonce=approval.nonce if nonce_present else None,
            not_before=unix(approval.created_at), max_age_s=stepup_max_age_seconds())
    except IdTokenError as exc:
        _transition(approval, "rejected", code=exc.code, detail=exc.message)
        db.session.commit()
        return approval
    except WorldError as exc:
        _transition(approval, "rejected", code=IDP_ERROR, detail=str(exc))
        db.session.commit()
        return approval
    if not nonce_present:
        _event(approval, "nonce_absent", binding="device_code")
    return _accept(approval, claims, id_token)


# ── common acceptance ───────────────────────────────────────────────────────

def _reject(approval: Approval, code: str, detail: str) -> Approval:
    _transition(approval, "rejected", code=code, detail=detail)
    db.session.commit()
    return approval


def _accept(approval: Approval, claims, id_token: str) -> Approval:
    """A validated ID token arrived for a pending, unexpired approval."""
    # Replay guard. Tokens without a jti are keyed by their own hash.
    replay_key = claims.jti or "sha256:" + hashlib.sha256(id_token.encode()).hexdigest()
    if db.session.get(UsedIdTokenJti, replay_key) is not None:
        return _reject(approval, REPLAYED_TOKEN, "id_token jti already used")
    try:
        with db.session.begin_nested():  # the primary key settles concurrent uses
            db.session.add(UsedIdTokenJti(jti=replay_key, seen_at=_now()))
    except IntegrityError:
        return _reject(approval, REPLAYED_TOKEN, "id_token jti already used")

    human = humans.upsert_human(claims.sub, now=_now())
    if human.banned:
        return _reject(approval, "BANNED", "this human is banned")
    buyer = _engagement_buyer(approval)
    if buyer is not None and buyer.id != human.id:
        return _reject(approval, "WRONG_HUMAN", "engagement is claimed by another human")

    approval.human_sub = claims.sub
    approval.id_token_jti = replay_key
    approval.auth_time = claims.auth_time
    approval.acr = claims.acr
    if approval.engagement_id and buyer is None:
        eng = db.session.get(Engagement, approval.engagement_id)
        if eng is not None:
            eng.buyer_human_id = human.id  # first approval claims the engagement
            _event(approval, "engagement_claimed", engagement_id=eng.id)
    _transition(approval, "approved", acr=claims.acr, auth_time=claims.auth_time)
    db.session.commit()
    return _auto_execute(approval)


def _auto_execute(approval: Approval) -> Approval:
    if approval.kind not in EXECUTORS:
        _event(approval, "awaiting_executor")
        db.session.commit()
        return approval
    try:
        consume(approval.id, kind=approval.kind)
    except ApprovalError:
        pass  # recorded on the approval (blocked / expired)
    return db.session.get(Approval, approval.id)


# ── cancel ──────────────────────────────────────────────────────────────────

def cancel(approval_id: str, reason: str = "") -> Approval:
    """Cancel a created or pending approval. Cancelling a cancelled approval
    is a no-op; any other state raises ApprovalStateError."""
    approval = _lock(approval_id)
    if approval.state == "cancelled":
        return approval
    if _expire_if_due(approval):
        db.session.commit()
        raise ApprovalStateError(approval.state, "approval has expired")
    if approval.state not in ("created", "pending"):
        raise ApprovalStateError(approval.state, f"approval is {approval.state}")
    approval.state_param = None
    approval.pkce_verifier = None
    _transition(approval, "cancelled", reason=(reason or "")[:500])
    db.session.commit()
    return approval


# ── consume ─────────────────────────────────────────────────────────────────

def consume(approval_id: str, *, kind: str) -> ExecutionResult:
    """Execute an approved action exactly once.

    Runs in one transaction holding the row lock: re-check, set consumed_at,
    run the executor inside a savepoint. Executor exceptions roll back only the
    executor's writes and leave the approval ``failed`` (no retry).
    """
    approval = _lock(approval_id)
    if approval.kind != kind:
        raise ApprovalError("HASH_MISMATCH", f"approval is for {approval.kind}, not {kind}")
    if approval.state == "consumed" or approval.consumed_at is not None:
        _event(approval, "replay_blocked", state=approval.state)
        db.session.commit()
        raise ApprovalError("APPROVAL_CONSUMED", "approval was already used")
    if _expire_if_due(approval):
        db.session.commit()
        raise ApprovalError("EXPIRED", "approval has expired")
    if approval.state != "approved":
        raise ApprovalError("NOT_APPROVED", f"approval is {approval.state}")
    fn = EXECUTORS.get(kind)
    if fn is None:
        raise LookupError(f"no executor registered for {kind!r}")

    action = approval.action
    try:
        if actions.action_hash(action) != approval.action_hash:
            raise ApprovalError("HASH_MISMATCH", "stored action does not match its hash")
        human = humans.get_by_sub(approval.human_sub or "")
        if human is None:
            raise ApprovalError("WRONG_HUMAN", "approval has no verified human")
        humans.ensure_not_banned(human)
        if kind in humans.CAPPED_KINDS:
            humans.check_weekly_cap(human, int(action.get("amount_micro") or 0), now=_now(),
                                    exclude_approval_id=approval.id)
        buyer = _engagement_buyer(approval)
        if buyer is not None and buyer.id != human.id:
            raise ApprovalError("WRONG_HUMAN", "engagement is claimed by another human")
        run_before_consume(approval, action)
        _check_screening(approval)
        _rescreen(approval, action)
    except ApprovalError as exc:
        _transition(approval, "blocked", code=exc.code, detail=exc.message)
        db.session.commit()
        raise

    approval.consumed_at = _now()
    db.session.flush()
    try:
        with db.session.begin_nested():
            result = fn(approval, action)
        if not isinstance(result, ExecutionResult):
            raise TypeError(f"executor for {kind} returned {type(result).__name__}")
    except Exception as exc:  # noqa: BLE001 - any executor failure fails the approval
        log.exception("executor %s failed for %s", kind, approval.id)
        result = ExecutionResult(ok=False, summary=f"{type(exc).__name__}: {exc}"[:500])
    if result.ok:
        _transition(approval, "consumed", result=_result_detail(result))
    else:
        _transition(approval, "failed", code=EXECUTOR_ERROR, detail=result.summary,
                    result=_result_detail(result))
    db.session.commit()
    if result.ok:
        run_after_consume(approval, action)
    return result


def _result_detail(result: ExecutionResult) -> dict:
    return {"ok": result.ok, "summary": result.summary, "ledger_ids": list(result.ledger_ids),
            "redirect": result.redirect}


# ── views ───────────────────────────────────────────────────────────────────

FAILURE_TEXT = {
    "NONCE_MISMATCH": "The sign-in was for a different action than this one.",
    REPLAYED_TOKEN: "This sign-in was already used once. Replays are blocked.",
    "WRONG_HUMAN": "A different person already owns this engagement.",
    "BANNED": "This account is not allowed to approve actions.",
    "CAP_EXCEEDED": "This would exceed the weekly spending limit.",
    "SCREENING_REFUSED": "The payee failed risk screening.",
    "HASH_MISMATCH": "The stored action no longer matches what was approved.",
    "STALE_AUTH": "The sign-in was not fresh enough. Please try again.",
    "ACR_MISMATCH": "The sign-in did not meet the required verification level.",
    "EXPIRED": "The identity token had expired.",
    "BAD_SIGNATURE": "The identity token signature was invalid.",
    "BAD_ALG": "The identity token used an unsupported algorithm.",
    "BAD_ISSUER": "The identity token came from an unexpected issuer.",
    "BAD_AUDIENCE": "The identity token was issued for another application.",
    "MISSING_CLAIM": "The identity token was missing required information.",
    IDP_ERROR: "The identity provider returned an error.",
    EXECUTOR_ERROR: "The approved action could not be completed.",
    "NOT_STAMPED": "The agent's configuration is no longer operator-stamped.",
    "RESTAMP_REQUIRED": "The agent's configuration changed after it was stamped.",
    "OPERATOR_BANNED": "The operator behind this agent is banned.",
    "PAYEE_REFUSED": "The agent's payout address was refused at onboarding.",
    "PAYEE_MISMATCH": "The ENS payee changed after approval, so the payment was blocked.",
    "PAYEE_UNRESOLVED": "The ENS payout record could not be resolved, so the payment was blocked.",
}


def failure_text(code: Optional[str]) -> str:
    return FAILURE_TEXT.get(code or "", code or "")


def result_of(approval: Approval) -> Optional[dict]:
    """The executor result recorded on the consumed / failed event."""
    for ev in reversed(approval.events):
        if ev.event in ("consumed", "failed") and ev.detail and "result" in ev.detail:
            return ev.detail["result"]
    return None


def replay_attempts(approval: Approval) -> int:
    return sum(1 for ev in approval.events if ev.event == "replay_blocked")


def to_dict(approval: Approval) -> dict:
    """The §7 approval object. Secrets (device_code, PKCE, state) never leave."""
    action = approval.action
    screening = None
    payer_screening = None
    if approval.screening_id:
        from app.screening.service import verdict_from_row
        row = db.session.get(Screening, approval.screening_id)
        screening = verdict_from_row(row) if row else {"id": approval.screening_id, "verdict": None}
        if action.get("payee_source"):
            screening = {**screening, "payee_source": action["payee_source"]}
    if action.get("payer_screening_id"):
        from app.screening.service import verdict_from_row
        row = db.session.get(Screening, action["payer_screening_id"])
        payer_screening = verdict_from_row(row) if row else {
            "id": action["payer_screening_id"], "verdict": None}
    result = result_of(approval)
    return {
        "approval_id": approval.id,
        "kind": approval.kind,
        "state": approval.state,
        "flow": approval.flow,
        "user_code": approval.user_code,
        "verification_uri": approval.verification_uri,
        "verification_uri_complete": approval.verification_uri_complete,
        "expires_at": unix(approval.expires_at),
        "action_hash": approval.action_hash,
        "summary": [list(row) for row in actions.describe(action)],
        "screening": screening,
        "payer_screening": payer_screening,
        "failure_code": approval.failure_code,
        "result": None if result is None else {
            "ledger_ids": result.get("ledger_ids", []), "tx": result.get("tx"),
            "summary": result.get("summary"),
        },
        "poll_interval": approval.poll_interval,
    }
