"""World OIDC routes for action-bound approvals.

The browser/device journey only changes an approval after the backend has
validated the World ID token. A client-side flag or wallet cookie is never
accepted as identity evidence.
"""
from __future__ import annotations

import secrets

from flask import current_app, jsonify, redirect, request, session

from app.approvals.service import ApprovalError, approve_from_claims, bind_device_code, get_approval
from app.identity import bp as identity_bp
from app.identity.world import WorldIdentityError, WorldOIDCClient, create_pkce_pair


def _error(message: str, code: str, status: int = 400):
    return jsonify({"error": message, "code": code}), status


def _client() -> WorldOIDCClient:
    configured = current_app.extensions.get("world_oidc_client")
    if configured is not None:
        return configured
    configured = WorldOIDCClient()
    current_app.extensions["world_oidc_client"] = configured
    return configured


def _approval_or_error(approval_id: str):
    row = get_approval(approval_id)
    if not row:
        return None, _error("approval not found", "APPROVAL_NOT_FOUND", 404)
    return row, None


@identity_bp.get("/start")
def start_world_login():
    approval_id = str(request.args.get("approval_id") or "")
    approval, error = _approval_or_error(approval_id)
    if error:
        return error
    if approval.state != "pending":
        return _error(f"approval is already {approval.state}", "APPROVAL_NOT_PENDING", 409)
    verifier, challenge = create_pkce_pair()
    state = secrets.token_urlsafe(32)
    # The action hash is the OIDC nonce. State and the PKCE verifier are
    # separate browser-session values used to protect the callback itself.
    session["world_auth"] = {
        "approval_id": approval.id,
        "state": state,
        "verifier": verifier,
    }
    try:
        location = _client().authorization_url(
            state=state, nonce=approval.action_hash, code_challenge=challenge)
    except WorldIdentityError as exc:
        session.pop("world_auth", None)
        return _error(str(exc), "WORLD_CONFIGURATION_ERROR", 503)
    return redirect(location)


@identity_bp.get("/callback")
def world_callback():
    pending = session.pop("world_auth", None) or {}
    if not pending or not secrets.compare_digest(
            str(request.args.get("state") or ""), str(pending.get("state") or "")):
        return _error("World callback state does not match", "WORLD_STATE_MISMATCH", 400)
    approval, error = _approval_or_error(str(pending.get("approval_id") or ""))
    if error:
        return error
    try:
        token_response = _client().exchange_code(
            str(request.args.get("code") or ""), str(pending.get("verifier") or ""))
        id_token = str(token_response.get("id_token") or "")
        if not id_token:
            raise WorldIdentityError("World token response has no ID token")
        claims = _client().validate_id_token(id_token, expected_nonce=approval.action_hash)
        row = approve_from_claims(approval.id, claims)
    except (WorldIdentityError, ApprovalError, KeyError, TypeError, ValueError) as exc:
        return _error(str(exc), "WORLD_APPROVAL_REJECTED", 403)
    return jsonify({"approvalId": row.id, "state": row.state, "actionHash": row.action_hash})


@identity_bp.post("/device")
def start_world_device_flow():
    body = request.get_json(silent=True) or {}
    approval, error = _approval_or_error(str(body.get("approvalId") or ""))
    if error:
        return error
    if approval.state != "pending":
        return _error(f"approval is already {approval.state}", "APPROVAL_NOT_PENDING", 409)
    try:
        device = _client().start_device_flow(action_hash=approval.action_hash)
        bind_device_code(approval.id, device.device_code)
    except (WorldIdentityError, ApprovalError) as exc:
        return _error(str(exc), "WORLD_DEVICE_ERROR", 503)
    return jsonify({
        "approvalId": approval.id,
        "actionHash": approval.action_hash,
        "deviceCode": device.device_code,
        "userCode": device.user_code,
        "verificationUri": device.verification_uri,
        "verificationUriComplete": device.verification_uri_complete,
        "expiresIn": device.expires_in,
        "interval": device.interval,
    }), 201


@identity_bp.post("/device/poll")
def poll_world_device_flow():
    body = request.get_json(silent=True) or {}
    approval, error = _approval_or_error(str(body.get("approvalId") or ""))
    if error:
        return error
    device_code = str(body.get("deviceCode") or "")
    if approval.state != "pending" or not device_code or approval.device_code != device_code:
        return _error("device approval is not bound to this pending action", "WORLD_DEVICE_MISMATCH", 400)
    try:
        token_response = _client().poll_device(device_code)
        if token_response.get("error") in {"authorization_pending", "slow_down"}:
            return jsonify({"approvalId": approval.id, "state": "pending",
                            "pollAfter": token_response.get("interval")})
        id_token = str(token_response.get("id_token") or "")
        if not id_token:
            raise WorldIdentityError("World device response has no ID token")
        # Device providers may omit nonce; the server-side device_code binding
        # in approve_from_claims still binds the validated subject to this action.
        claims = _client().validate_id_token(id_token)
        row = approve_from_claims(approval.id, claims, device_code=device_code)
    except (WorldIdentityError, ApprovalError, KeyError, TypeError, ValueError) as exc:
        return _error(str(exc), "WORLD_DEVICE_REJECTED", 403)
    return jsonify({"approvalId": row.id, "state": row.state, "actionHash": row.action_hash})
