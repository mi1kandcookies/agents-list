"""Agent-to-agent paid tasks over x402 v2.

    POST /api/agents/<AGT>/tasks            {"task": "..."}
      no payment header        → 402, x402 v2 PaymentRequired (body and PAYMENT-REQUIRED header)
      X-PAYMENT + Authorization: Mandate <jwt>
                               → 200 task receipt (PAYMENT-RESPONSE header)

A paying agent spends under the mandate it was granted (§5), so every
payment still traces back to one human approval. In order: the payment is
verified against this server's requirements and the token's EIP-712 domain;
the mandate chain is re-verified and the payer's wallet must belong to the
mandate's grantee; the payment policy caps the amount (per-transaction cap,
remaining budget, X402_MAX_PAYMENT_USDC) and the payees; the payer is deep-
screened (hop ``payer.check``) before the payee and exact authorization are
screened (hop ``subhire.hop``, fail closed); the nonce is burned; the
mandate is charged; then the authorization is settled through the escrow
service's facilitator path (simulated without keys) and recorded in the
root engagement's ledger. The callee must be hireable (operator-stamped
manifest, ``assert_hireable``); its price is the stamped ``price_min_micro``.
The payee comes from ``resolve_payee`` (ENS or profile; a mismatch refuses).

On chain, settlement can come back ``pending``. When receipt polling
(``ledger.refresh_receipts``) later finds that transaction failed,
``settlement_failed`` marks the row failed and gives the charge back to the
mandate, exactly once per row.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from flask import current_app, jsonify, request
from sqlalchemy import update

from app.api.routes import bp
from app.extensions import db, limiter
from app.services import api_error
from chain import x402_v2
from chain.payment_policy import NonceRegistry, PaymentPolicy, PolicyError
from chain.x402_v2 import X402Error

log = logging.getLogger(__name__)

HOP = "subhire.hop"
MAX_TASK_CHARS = 4000
HIRE_INTENT_HEADER = "X-HIRE-INTENT"
HIRE_INTENT_HASH_HEADER = "X-HIRE-INTENT-HASH"


def _nonces() -> NonceRegistry:
    return current_app.extensions.setdefault("agents_list.x402_nonces", NonceRegistry())


def _usdc_setting(name: str) -> int:
    from app.engagements.sow import parse_usdc
    return parse_usdc(current_app.config.get(name), name)


def _price_micro(agent) -> int:
    """The operator-stamped manifest's ``price_min_micro`` (the caller has
    already checked the stamp covers the current manifest), else
    AGENT_TASK_PRICE_USDC."""
    from app.seller.stamp import current_manifest
    price = (current_manifest(agent) or {}).get("price_min_micro")
    if isinstance(price, int) and not isinstance(price, bool) and price > 0:
        return price
    return _usdc_setting("AGENT_TASK_PRICE_USDC")


def _payment_required(req, *, error: str, code: str | None = None, intent=None):
    from app.hiring import intents
    body = x402_v2.payment_required(req, resource_url=request.path, error=error,
                                    description="Run one task with this agent",
                                    extensions=intents.extension(intent) if intent else None)
    if code:
        body["code"] = code
    resp = jsonify(body)
    resp.status_code = 402
    resp.headers[x402_v2.REQUIRED_HEADER] = x402_v2.encode_header(body)
    if intent is not None:
        resp.headers[HIRE_INTENT_HEADER] = intent.id
        resp.headers[HIRE_INTENT_HASH_HEADER] = intent.intent_hash
        resp.headers["X-HIRE-TASK-HASH"] = intent.task_hash
    return resp


def _payment_header() -> str | None:
    for name in x402_v2.PAYMENT_HEADERS:
        value = request.headers.get(name)
        if value:
            return value
    return None


def _mandate_token() -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme == "Mandate" and token.strip() else None


def _unspend(mandate_id: str, amount: int) -> bool:
    """Give back a charge whose settlement failed (never below zero). Caller
    commits. Returns whether the mandate was updated."""
    from app.models import Mandate
    result = db.session.execute(
        update(Mandate).where(Mandate.id == mandate_id, Mandate.spent_micro >= amount)
        .values(spent_micro=Mandate.spent_micro - amount))
    return result.rowcount == 1


def _charged_mandate(entry):
    """The mandate a task payment row was charged to: a mandate on the row's
    engagement granted to the agent whose wallet signed the payment (the
    payer is checked against exactly these wallets before charging). If that
    agent holds several there, the newest one that covers the amount."""
    from sqlalchemy import func, or_
    from app.models import Agent, Mandate
    payer = (entry.from_addr or "").lower()
    if not payer:
        return None
    grantees = [a.public_id for a in Agent.query.filter(or_(
        func.lower(Agent.payout_address) == payer,
        func.lower(Agent.deployer_wallet) == payer)).all() if a.public_id]
    if not grantees:
        return None
    return (Mandate.query.filter(Mandate.engagement_id == entry.engagement_id,
                                 Mandate.grantee_agent_public_id.in_(grantees),
                                 Mandate.spent_micro >= entry.amount_micro)
            .order_by(Mandate.expires_at.desc(), Mandate.id.desc()).first())


def settlement_failed(entry) -> bool:
    """Receipt polling found a task payment's transaction failed: mark the
    ledger row ``failed`` and refund the mandate charge. Idempotent: the row
    moves ``pending → failed`` with a conditional UPDATE, and only the caller
    that wins it refunds, so repeated or concurrent polls refund once.
    Caller commits. Returns whether this call changed the row."""
    from app.models import HireIntent, LedgerEntry
    result = db.session.execute(
        update(LedgerEntry).where(LedgerEntry.id == entry.id, LedgerEntry.status == "pending")
        .values(status="failed").execution_options(synchronize_session=False))
    db.session.refresh(entry)
    if result.rowcount != 1:
        return False
    intent = HireIntent.query.filter_by(payment_entry_id=entry.id).first()
    if intent is not None:
        intent.state = "failed"
        intent.failure_code = "SETTLEMENT_FAILED"
        intent.updated_at = datetime.now(timezone.utc)
    row = _charged_mandate(entry)
    if row is None or not _unspend(row.id, int(entry.amount_micro)):
        log.warning("x402 payment %s failed on chain; no mandate charge found to refund", entry.id)
    else:
        log.info("x402 payment %s failed on chain; refunded %d to %s",
                 entry.id, entry.amount_micro, row.id)
    return True


@bp.route("/agents/<agent_ref>/tasks", methods=["POST"])
@limiter.limit("30/minute")
def api_agent_task(agent_ref):
    from app.engagements import ledger
    from app.engagements import service as eng_svc
    from app.engagements.service import EngagementError
    from app.hiring.deliverables import build_api_test_plan
    from app.mandates import service as mandates
    from app.mandates.tokens import MandateError
    from app.models import Agent, Engagement, HireIntent, Mandate
    from app.screening import policy as screening_policy
    from app.hiring import intents

    from app.seller.stamp import assert_hireable

    try:
        agent = eng_svc.resolve_agent(agent_ref)
        assert_hireable(agent)      # stamped manifest, operator not banned, payee not refused
        payee = eng_svc.resolve_payee(agent)
    except EngagementError as exc:
        return api_error(exc.message, exc.status, code=exc.code, field=exc.field)
    if payee.address is None:
        return api_error("agent has no payout address", 409, code="PAYEE_ADDRESS_MISSING")
    body = request.get_json(silent=True)
    requested_task = str(body.get("task") or "").strip() if isinstance(body, dict) else ""
    body_intent_id = str(body.get("intent_id") or "").strip() if isinstance(body, dict) else ""
    header_intent_id = request.headers.get(HIRE_INTENT_HEADER, "").strip()
    if body_intent_id and header_intent_id and body_intent_id != header_intent_id:
        return api_error("body and header intent ids differ", 409, code="HIRE_INTENT_MISMATCH")
    intent_id = header_intent_id or body_intent_id
    try:
        amount = _price_micro(agent)
        req = x402_v2.build_requirements(pay_to=payee.address, amount_micro=amount)
    except X402Error as exc:
        return api_error(exc.message, 503, code=exc.code)

    header = _payment_header()
    if header and not intent_id:
        return api_error("the payment must reference the server-issued hire intent", 400,
                         code="HIRE_INTENT_REQUIRED")
    intent = None
    if intent_id:
        intent = db.session.get(HireIntent, intent_id)
        if intent is None:
            return api_error("hire intent was not found", 404, code="HIRE_INTENT_NOT_FOUND")
        if intent.agent_id != agent.id:
            return api_error("hire intent belongs to another specialist", 409,
                             code="HIRE_INTENT_AGENT_MISMATCH")
        if intent.is_expired():
            if intent.state == "created":
                intent.state = "expired"
                intent.failure_code = "INTENT_EXPIRED"
                db.session.commit()
            return api_error("hire intent has expired", 409, code="HIRE_INTENT_EXPIRED")
        if intent.state != "created":
            code = "PAYMENT_PENDING" if intent.state == "payment_pending" else "HIRE_INTENT_USED"
            return api_error(f"hire intent is already {intent.state}", 409, code=code)
        if not intents.matches_snapshot(
                intent, agent_public_id=agent.public_id, ens_name=payee.ens_name,
                endpoint_path=request.path, network=req.network, token_address=req.asset,
                pay_to=payee.address, amount_micro=amount):
            return api_error("ENS, endpoint, payment terms, or agent identity changed", 409,
                             code="HIRE_INTENT_STALE")
        supplied_hash = request.headers.get(HIRE_INTENT_HASH_HEADER, "").strip()
        if supplied_hash and supplied_hash != intent.intent_hash:
            return api_error("hire intent hash does not match", 409, code="HIRE_INTENT_HASH_MISMATCH")
        if requested_task and intents.task_hash(requested_task) != intent.task_hash:
            return api_error("the requested task differs from the approved intent", 409,
                             code="INTENT_TASK_MISMATCH")
        task = intent.task_text
    else:
        if not requested_task or len(requested_task) > MAX_TASK_CHARS:
            return api_error(f"task is required (at most {MAX_TASK_CHARS} characters)", field="task")
        task = requested_task
        intent = intents.create(
            agent=agent, ens_name=payee.ens_name, endpoint_path=request.path, task=task,
            network=req.network, token_address=req.asset, pay_to=payee.address,
            amount_micro=amount)
        db.session.add(intent)
        db.session.commit()

    if not header:
        return _payment_required(req, error="X-PAYMENT header is required", intent=intent)
    if intent is None:
        return api_error("a hire intent is required", 400, code="HIRE_INTENT_REQUIRED")
    token = _mandate_token()
    if token is None:
        return api_error("a mandate is required: Authorization: Mandate <token>", 401,
                         code="MANDATE_INVALID")
    try:
        verified = x402_v2.verify_payment(x402_v2.decode_header(header), req)
    except X402Error as exc:
        return _payment_required(req, error=exc.message, code=exc.code)

    try:
        claims = mandates.verify_chain(token)
    except MandateError as exc:
        return api_error(exc.message, 403, code=exc.code)
    payer_agent = Agent.query.filter_by(public_id=claims["sub"]).first()
    if payer_agent is None or payer_agent.id == agent.id:
        return api_error("the mandate's agent cannot pay this task", 403, code="MANDATE_INVALID")
    wallets = {w.lower() for w in (payer_agent.payout_address, payer_agent.deployer_wallet) if w}
    if verified.payer not in wallets:
        return api_error("the payment is not signed by the mandate's agent", 403,
                         code="PAYER_NOT_MANDATED")
    row = db.session.get(Mandate, claims["jti"])
    cap = claims.get("cap") or {}
    try:
        PaymentPolicy(
            min(_usdc_setting("X402_MAX_PAYMENT_USDC"), int(cap.get("per_tx_max_micro") or 0),
                mandates.remaining_micro(row)),
            cap.get("payees"),
        ).check(amount_micro=verified.amount_micro, pay_to=verified.pay_to)
    except PolicyError as exc:
        return api_error(exc.message, 403, code=exc.code)

    engagement = db.session.get(Engagement, row.engagement_id)

    # The specialist is also a payee-side gatekeeper: before accepting a
    # sub-hire, it deep-scans the payer wallet. This is distinct from the
    # payer-side pre-sign hook, which screens the payee before the buyer's
    # signer is invoked.
    payer_verdict = eng_svc.screen("payer.check", chain_address=verified.payer,
                                   amount_micro=amount, engagement=engagement,
                                   agent=payer_agent, typed_data=verified.typed_data)
    try:
        screening_policy.enforce_verdict(payer_verdict, amount)
    except screening_policy.ScreeningBlocked as exc:
        db.session.commit()
        return jsonify({"error": f"payer refused by risk screening ({exc.code})",
                        "code": "SCREENING_REFUSED",
                        "screening": eng_svc.screening_json(payer_verdict)}), 403

    # Screen the destination and exact EIP-712 authorization on the
    # specialist hop as well. Both verdicts must allow the transfer.
    verdict = eng_svc.screen(HOP, chain_address=verified.pay_to, amount_micro=amount,
                             engagement=engagement, agent=agent,
                             typed_data=verified.typed_data)
    try:
        screening_policy.enforce_verdict(verdict, amount)
    except screening_policy.ScreeningBlocked as exc:
        db.session.commit()
        return jsonify({"error": f"payment refused by risk screening ({exc.code})",
                        "code": "SCREENING_REFUSED",
                        "screening": eng_svc.screening_json(verdict)}), 403

    # Claim the server-owned intent once, before charging the mandate. This is
    # the concurrency/idempotency boundary: only one payment attempt can bind
    # the approved task, payee, and nonce to this intent.
    claim = db.session.execute(
        update(HireIntent)
        .where(HireIntent.id == intent.id, HireIntent.state == "created")
        .values(payer_agent_public_id=payer_agent.public_id, mandate_id=row.id,
                payment_nonce=verified.nonce, state="payment_pending",
                updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False))
    if claim.rowcount != 1:
        db.session.rollback()
        return api_error("hire intent has already been claimed", 409, code="HIRE_INTENT_USED")
    db.session.refresh(intent)

    # Build the read-only result before settlement, but do not publish it until
    # the transfer succeeds below. The source text is hashed into the result;
    # there is no second, unapproved task body accepted after payment.
    deliverable = build_api_test_plan(task, specialist=agent.name,
                                      agent_id=agent.public_id)

    nonces = _nonces()
    try:
        nonces.claim(verified.nonce)
    except PolicyError as exc:
        db.session.rollback()
        return api_error(exc.message, 409, code=exc.code)
    try:
        mandates.spend(row.id, amount, category=agent.category)
    except MandateError as exc:
        nonces.release(verified.nonce)
        return api_error(exc.message, 403, code=exc.code)

    try:
        tx = x402_v2.settle(verified, escrow=eng_svc.get_escrow(), ref=f"x402:{verified.nonce}")
    except Exception as exc:
        log.warning("x402 settlement failed for %s: %s", agent.public_id, str(exc)[:200])
        _unspend(row.id, amount)
        entry = ledger.record(engagement, kind="subhire_alloc", amount_micro=amount, status="failed",
                              from_addr=verified.payer, to_addr=verified.pay_to,
                              approval_id=claims.get("apr"), screening_id=verdict["id"])
        intent.state = "failed"
        intent.failure_code = "PAYMENT_FAILED"
        intent.payment_entry_id = entry.id
        db.session.commit()
        return api_error("payment settlement failed", 502, code="PAYMENT_FAILED")
    entry = ledger.record(engagement, kind="subhire_alloc", amount_micro=amount, tx=tx,
                          from_addr=verified.payer, to_addr=verified.pay_to,
                          approval_id=claims.get("apr"), screening_id=verdict["id"])
    intent.payment_entry_id = entry.id
    intent.deliverable_json = json.dumps(deliverable, sort_keys=True, separators=(",", ":"))
    intent.state = "delivered" if tx.status in ("simulated", "confirmed") else "payment_pending"
    db.session.commit()

    settle = x402_v2.settle_response(verified, tx)
    result = {
        "receipt_id": entry.id,
        "status": "accepted" if intent.state == "delivered" else "payment_pending",
        "agent_id": agent.public_id,
        "payer_agent_id": payer_agent.public_id,
        "task_hash": "0x" + hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "intent": intent.to_dict(),
        "mandate_id": row.id,
        "engagement_id": engagement.id,
        "payment": {**ledger.entry_json(entry), "network": verified.network,
                    "payer": verified.payer, "pay_to": verified.pay_to,
                    "payee_source": payee.source, "nonce": verified.nonce},
        "screening": eng_svc.screening_json(verdict),
        "payer_screening": eng_svc.screening_json(payer_verdict),
    }
    if intent.state == "delivered":
        result["deliverable"] = deliverable
    resp = jsonify(result)
    encoded = x402_v2.encode_header(settle)
    for name in x402_v2.RESPONSE_HEADERS:
        resp.headers[name] = encoded
    return resp
