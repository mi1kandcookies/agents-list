"""Identity routes: sign-in (a ``session.login`` step-up approval), the World
ID callback for every approval kind, and sign-out."""
from __future__ import annotations

from flask import redirect, render_template, request, session, url_for

from app.approvals import service
from app.identity import bp
from app.identity.session import (LOGIN_APPROVAL_KEY, LOGIN_NEXT_KEY, current_human, logout,
                                  remember_web_state, safe_next, take_web_state)
from app.identity.world import WorldError


def _error_page(title: str, message: str, status: int):
    return render_template("approvals/error.html", title=title, message=message), status


@bp.get("/login")
def login():
    nxt = safe_next(request.args.get("next"))
    if current_human() is not None:
        return redirect(nxt)
    client = service.world_client()
    if not (client.configured and client.redirect_uri):
        return _error_page("Sign-in not configured",
                           "World ID sign-in is not set up on this server.", 200)
    approval = service.create_approval("session.login", {}, flow="web")
    session[LOGIN_APPROVAL_KEY] = approval.id
    session[LOGIN_NEXT_KEY] = nxt
    try:
        url = service.start_web(approval)
    except WorldError:
        return _error_page("Sign-in unavailable",
                           "The identity provider could not be reached. Try again.", 503)
    remember_web_state(approval.state_param, approval.id)
    return redirect(url)


@bp.get("/auth/world/callback")
def world_callback():
    state = request.args.get("state", "")
    approval_id = take_web_state(state) if state else None
    if approval_id is None:
        # Unknown here: never started in this browser, already used, or forged.
        return _error_page("Sign-in link not valid here",
                           "This sign-in was not started in this browser, or it was already "
                           "used. Start again from the approval page.", 400)
    try:
        approval = service.complete_web(state, request.args.get("code"), request.args.get("error"))
    except service.ApprovalNotFound:
        return _error_page("Sign-in link expired",
                           "This sign-in link is no longer valid. Start again.", 400)
    if approval.id != approval_id:
        return _error_page("Sign-in link not valid here", "State mismatch.", 400)
    if approval.state == "consumed":
        result = service.result_of(approval) or {}
        if result.get("redirect"):
            return redirect(safe_next(result["redirect"]))
    return redirect(url_for("approvals.status", approval_id=approval.id))


@bp.route("/logout", methods=["GET", "POST"])
def logout_view():
    logout()
    return redirect("/")
