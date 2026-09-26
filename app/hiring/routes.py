"""API for the protected ENS → screening → approval → x402 hire journey."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app.extensions import db, limiter
from app.hiring.deliverables import generate_test_plan
from app.hiring.services import (
    create_intent, get_intent, get_validated_approval, hold_intent, record_local_decision,
    record_validated_approval, request_approval, status_payload, claim_payment, deliver_intent,
    new_id,
)
from chain.ens_v2 import ENSResolutionError
from chain.payment_policy import PaymentPolicyError, canonical_json, screen_and_validate
from chain.x402_v2 import (
    payment_required_for_intent, requirements_for_intent,
    settle_payment, verify_payment,
)

log = logging.getLogger(__name__)
bp = Blueprint("hiring", __name__, url_prefix="/api/hiring")


def _json_error(message: str, status=400, code="INVALID_REQUEST"):
    return jsonify({"error": message, "code": code}), status


def _encode_payment_required(payment_required):
    from x402.http.utils import encode_payment_required_header
    return encode_payment_required_header(payment_required)


def _add_payment_response_header(response, receipt: dict, intent) -> None:
    from x402.http.utils import encode_payment_response_header
    from x402.schemas import SettleResponse
    response.headers["PAYMENT-RESPONSE"] = encode_payment_response_header(SettleResponse(
        success=True, transaction=receipt.get("transaction", ""),
        network=receipt.get("network") or intent.network, payer=receipt.get("payer") or intent.payer,
        amount=str(intent.amount_atomic),
    ))


def _fingerprint(payload) -> str:
    return hashlib.sha256(canonical_json(payload.model_dump(by_alias=True, exclude_none=True)).encode()).hexdigest()


def _expiry(value: str):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        raise ValueError("expiresAt must be an ISO-8601 timestamp") from None


@bp.route("/intents", methods=["POST"])
@limiter.limit("30/minute")
def create_hire_intent():
    body = request.get_json(silent=True) or {}
    try:
        intent = create_intent(
            owner_id=body.get("ownerId"), payer=body.get("payer"),
            specialist_name=body.get("agent"), task=body.get("task"),
            max_usdc=body.get("maxUsdc"),
        )
    except (ValueError, ENSResolutionError) as exc:
        return _json_error(str(exc), code="INTENT_REJECTED")
    data = status_payload(intent)
    if intent.status in {"screening_hold", "denied"}:
        return jsonify(data), 409
    return jsonify(data), 201


@bp.route("/intents/<intent_id>")
def get_hire_intent(intent_id):
    intent = get_intent(intent_id)
    if not intent:
        return _json_error("intent not found", 404, "INTENT_NOT_FOUND")
    return jsonify(status_payload(intent))


@bp.route("/intents/<intent_id>/approval", methods=["POST"])
def create_approval(intent_id):
    intent = get_intent(intent_id)
    if not intent:
        return _json_error("intent not found", 404, "INTENT_NOT_FOUND")
    approval = request_approval(intent)
    if not approval:
        return _json_error("approval cannot be requested in this intent state", 409, "INVALID_STATE")
    return jsonify(approval.to_dict()), 201


@bp.route("/approvals/<approval_id>/decision", methods=["POST"])
def local_approval_decision(approval_id):
    """Local-only stand-in for the teammate-owned World approval screen."""
    if str(__import__("os").environ.get("HIRE_LOCAL_APPROVAL", "")).lower() not in {"1", "true", "yes"}:
        return _json_error("local approval adapter is disabled", 404, "NOT_FOUND")
    from app.models import HireApproval
    approval = db.session.get(HireApproval, approval_id)
    if not approval:
        return _json_error("approval not found", 404, "APPROVAL_NOT_FOUND")
    body = request.get_json(silent=True) or {}
    try:
        row = record_local_decision(
            approval, owner_id=str(body.get("ownerId") or ""),
            state=str(body.get("state") or ""), reason=str(body.get("reason") or ""),
        )
    except (PermissionError, ValueError) as exc:
        return _json_error(str(exc), 403 if isinstance(exc, PermissionError) else 400,
                           "APPROVAL_REJECTED")
    return jsonify(row.to_dict())


@bp.route("/approvals/<approval_id>/validated", methods=["POST"])
def validated_approval(approval_id):
    """Callback consumed by the World identity/consent service.

    The service key is separate from the buyer wallet and must never be a
    browser flag. The teammate owns the proof validation before calling this.
    """
    import os
    expected_key = os.environ.get("APPROVAL_SERVICE_KEY", "").strip()
    if not expected_key or request.headers.get("X-Approval-Service-Key") != expected_key:
        return _json_error("approval service authentication failed", 403, "FORBIDDEN")
    from app.models import HireApproval
    approval = db.session.get(HireApproval, approval_id)
    if not approval:
        return _json_error("approval not found", 404, "APPROVAL_NOT_FOUND")
    body = request.get_json(silent=True) or {}
    try:
        row = record_validated_approval(
            approval, owner_id=str(body.get("ownerId") or ""),
            payer=str(body.get("payer") or ""), intent_hash=str(body.get("intentHash") or ""),
            expires_at=_expiry(str(body.get("expiresAt") or "")),
            proof_id=str(body.get("proofId") or ""),
        )
    except (PermissionError, ValueError) as exc:
        return _json_error(str(exc), 403 if isinstance(exc, PermissionError) else 400,
                           "APPROVAL_REJECTED")
    return jsonify(row.to_dict())


@bp.route("/intents/<intent_id>/work", methods=["POST"])
@limiter.limit("15/minute")
def protected_work(intent_id):
    """Protected deliverable route. The task only comes from the intent row."""
    intent = get_intent(intent_id)
    if not intent:
        return _json_error("intent not found", 404, "INTENT_NOT_FOUND")
    approval, approval_reason = get_validated_approval(intent)
    if not approval:
        return _json_error(approval_reason, 403, "APPROVAL_REQUIRED")
    try:
        screening = screen_and_validate(intent)
    except PaymentPolicyError as exc:
        hold_intent(intent, str(exc), status="denied" if exc.code == "INTERCEPTA_DENY" else "held")
        return _json_error(str(exc), 403 if exc.code == "INTERCEPTA_DENY" else 409, exc.code)
    intent.screening = json.dumps(screening.to_dict(), sort_keys=True)
    db.session.commit()

    payment_header = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("payment-signature")
    attempt_header = request.headers.get("X-Hire-Attempt")
    demo_payment = request.headers.get("X-Hire-Demo-Payment") == "1"
    if not payment_header and not (demo_payment and __import__("os").environ.get("HIRE_PAYMENT_MODE") == "mock"):
        required = payment_required_for_intent(intent)
        response = jsonify({"error": "Payment required", "code": "PAYMENT_REQUIRED",
                            "intentId": intent.id,
                            "paymentRequired": required.model_dump(by_alias=True, exclude_none=True)})
        response.status_code = 402
        response.headers["PAYMENT-REQUIRED"] = _encode_payment_required(required)
        response.headers["Cache-Control"] = "no-store"
        return response

    if demo_payment:
        if __import__("os").environ.get("HIRE_PAYMENT_MODE") != "mock":
            return _json_error("demo payment mode is disabled", 403, "PAYMENT_DISABLED")
        try:
            claim_payment(intent, f"demo-{intent.id}")
            result = generate_test_plan(intent.task, specialist_name=intent.specialist_name)
            receipt = {"status": "settled", "transaction": f"demo-{intent.id}",
                       "network": intent.network, "payer": intent.payer,
                       "provider": "local-demo-only"}
            deliver_intent(intent, receipt, result)
            return jsonify({"intent": status_payload(intent), "screening": screening.to_dict(),
                            "approval": approval.to_dict(), "receipt": receipt, "result": result})
        except ValueError as exc:
            return _json_error(str(exc), 409, "DUPLICATE_PAYMENT")

    preclaimed = intent.status == "payment_pending" and attempt_header and attempt_header == intent.payment_fingerprint
    if intent.status == "payment_pending" and not preclaimed:
        return _json_error("payment attempt is already claimed or belongs to another runner", 409,
                           "DUPLICATE_PAYMENT")
    try:
        from x402.http.utils import decode_payment_signature_header
        payload = decode_payment_signature_header(payment_header)
        requirements = requirements_for_intent(intent)
        if payload.x402_version != 2 or payload.accepted is None:
            return _json_error("only x402 v2 exact payments are accepted", 402, "UNSUPPORTED_PAYMENT")
        from chain.payment_policy import validate_payer, validate_requirements
        validate_requirements(intent, payload.accepted)
        auth = (payload.payload or {}).get("authorization") or {}
        validate_payer(intent, str(auth.get("from") or ""))
    except (ValueError, TypeError, PaymentPolicyError) as exc:
        return _json_error(str(exc), 402, getattr(exc, "code", "INVALID_PAYMENT"))

    if not preclaimed:
        try:
            claim_payment(intent, _fingerprint(payload))
        except ValueError as exc:
            return _json_error(str(exc), 409, "DUPLICATE_PAYMENT")
    else:
        intent.payment_fingerprint = _fingerprint(payload)
        db.session.commit()
    verification = verify_payment(payload, requirements)
    if not verification.success:
        hold_intent(intent, verification.error or "x402 verification failed")
        return _json_error(verification.error or "payment verification failed", 402, "PAYMENT_VERIFY_FAILED")
    if verification.payer and verification.payer.lower() != intent.payer.lower():
        hold_intent(intent, "x402 facilitator payer does not match the approved payer")
        return _json_error("facilitator payer does not match the approved payer", 402, "PAYER_CHANGED")
    try:
        result = generate_test_plan(intent.task, specialist_name=intent.specialist_name)
    except Exception as exc:
        hold_intent(intent, f"deliverable generation failed: {str(exc)[:180]}")
        return _json_error("deliverable generation failed before settlement", 502, "DELIVERY_FAILED")
    receipt = settle_payment(payload, requirements).to_dict()
    if receipt["status"] != "settled":
        hold_intent(intent, receipt.get("error") or "settlement is pending", status="settlement_pending")
        return _json_error(receipt.get("error") or "settlement pending", 503, "SETTLEMENT_PENDING")
    deliver_intent(intent, receipt, result)
    response = jsonify({"intent": status_payload(intent), "screening": screening.to_dict(),
                        "approval": approval.to_dict(), "receipt": receipt, "result": result})
    _add_payment_response_header(response, receipt, intent)
    return response


@bp.route("/intents/<intent_id>/preflight")
def preflight(intent_id):
    """Final read-only gate called immediately before the buyer signer."""
    intent = get_intent(intent_id)
    if not intent:
        return _json_error("intent not found", 404, "INTENT_NOT_FOUND")
    approval, approval_reason = get_validated_approval(intent)
    if not approval:
        return _json_error(approval_reason, 403, "APPROVAL_REQUIRED")
    try:
        screening = screen_and_validate(intent)
    except PaymentPolicyError as exc:
        hold_intent(intent, str(exc), status="denied" if exc.code == "INTERCEPTA_DENY" else "held")
        return _json_error(str(exc), 403 if exc.code == "INTERCEPTA_DENY" else 409, exc.code)
    required = payment_required_for_intent(intent)
    return jsonify({
        "intentId": intent.id,
        "approval": approval.to_dict(),
        "screening": screening.to_dict(),
        "paymentRequired": required.model_dump(by_alias=True, exclude_none=True),
    })


@bp.route("/intents/<intent_id>/claim", methods=["POST"])
def claim_hire_attempt(intent_id):
    """Atomically reserve the one payment attempt immediately before signing."""
    intent = get_intent(intent_id)
    if not intent:
        return _json_error("intent not found", 404, "INTENT_NOT_FOUND")
    approval, approval_reason = get_validated_approval(intent)
    if not approval:
        return _json_error(approval_reason, 403, "APPROVAL_REQUIRED")
    try:
        screening = screen_and_validate(intent)
        attempt_id = new_id("ATTEMPT")
        claimed = claim_payment(intent, attempt_id)
    except PaymentPolicyError as exc:
        hold_intent(intent, str(exc), status="denied" if exc.code == "INTERCEPTA_DENY" else "held")
        return _json_error(str(exc), 403 if exc.code == "INTERCEPTA_DENY" else 409, exc.code)
    except ValueError as exc:
        return _json_error(str(exc), 409, "DUPLICATE_PAYMENT")
    return jsonify({"intentId": claimed.id, "attemptId": attempt_id,
                    "screening": screening.to_dict(), "approval": approval.to_dict()})
