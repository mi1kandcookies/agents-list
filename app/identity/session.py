"""Browser sessions for verified humans.

A session is created only by the ``session.login`` executor, i.e. after a
fresh World ID authentication approved a ``session.login`` action. The
executor logs in only the browser that started that login (the approval id is
bound in its session by GET /login), so a login link or callback forwarded to
someone else cannot sign them in.
"""
from __future__ import annotations

import functools
import hmac
from typing import Optional
from urllib.parse import urlsplit

from flask import g, has_request_context, jsonify, redirect, request, session, url_for

from app.approvals.executors import ExecutionResult, executor
from app.extensions import db
from app.humans import service as humans
from app.models import Approval, Human

SESSION_KEYS = ("human_id", "human_sub", "login_at")
LOGIN_APPROVAL_KEY = "login_approval_id"
LOGIN_NEXT_KEY = "login_next"
# state → approval id for web flows started in this browser (bounded).
WEB_STATES_KEY = "world_web_states"
MAX_WEB_STATES = 5


def safe_next(target: Optional[str]) -> str:
    """Only same-site relative paths; anything else becomes '/'."""
    if not target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//") \
            or "\\" in target:
        return "/"
    return target


def remember_web_state(state: str, approval_id: str) -> None:
    """Bind a web-flow ``state`` to this browser; the callback requires it."""
    states = dict(session.get(WEB_STATES_KEY) or {})
    states[state] = approval_id
    session[WEB_STATES_KEY] = dict(list(states.items())[-MAX_WEB_STATES:])


def take_web_state(state: str) -> Optional[str]:
    """Pop and return the approval id bound to ``state`` in this browser."""
    states = dict(session.get(WEB_STATES_KEY) or {})
    for known in list(states):
        if hmac.compare_digest(known, state):
            approval_id = states.pop(known)
            session[WEB_STATES_KEY] = states
            return approval_id
    return None


def current_human() -> Optional[Human]:
    """The signed-in, non-banned Human, or None."""
    if not has_request_context():
        return None
    if "current_human" in g:
        return g.current_human
    human = None
    human_id = session.get("human_id")
    if human_id is not None:
        human = db.session.get(Human, human_id)
        if human is None or human.world_sub != session.get("human_sub") or human.banned:
            logout()
            human = None
    g.current_human = human
    return human


def login(human: Human, *, at: int) -> None:
    session.pop(LOGIN_APPROVAL_KEY, None)
    for key in SESSION_KEYS:
        session.pop(key, None)
    session["human_id"] = human.id
    session["human_sub"] = human.world_sub
    session["login_at"] = at
    g.current_human = human


def logout() -> None:
    for key in SESSION_KEYS + (LOGIN_APPROVAL_KEY, LOGIN_NEXT_KEY):
        session.pop(key, None)
    g.pop("current_human", None)


def login_required(view):
    """Redirect anonymous browsers to /login (401 JSON for /api/ paths)."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_human() is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "sign-in required", "code": "LOGIN_REQUIRED",
                                "field": None}), 401
            return redirect(url_for("identity.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


@executor("session.login")
def execute_login(approval: Approval, action: dict) -> ExecutionResult:
    if not has_request_context() or session.get(LOGIN_APPROVAL_KEY) != approval.id:
        return ExecutionResult(ok=False, summary="sign-in was not started in this browser")
    human = humans.get_by_sub(approval.human_sub or "")
    if human is None:
        return ExecutionResult(ok=False, summary="no verified human")
    login(human, at=int(approval.auth_time or 0))
    return ExecutionResult(ok=True, summary="Signed in",
                           redirect=safe_next(session.pop(LOGIN_NEXT_KEY, None)))
