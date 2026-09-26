"""Routes for the intake blueprint."""
from __future__ import annotations

from flask import abort, render_template, request

from app.extensions import db
from app.intake import bp
from app.intake.estimate import AUTO_RELEASE_DAYS, BUFFER, CATEGORIES, estimate

MAX_PREFILL = 500


def flow_config() -> dict:
    """Everything flow.js needs that should not be duplicated in JavaScript."""
    return {"categories": CATEGORIES, "buffer": BUFFER,
            "auto_release_days": AUTO_RELEASE_DAYS}


@bp.route("/new")
def new_job():
    prefill = (request.args.get("q") or "").strip()[:MAX_PREFILL]
    return render_template("intake/new.html", prefill=prefill, config=flow_config(),
                           auto_release_days=AUTO_RELEASE_DAYS)


def _criteria(acceptance: str) -> list[str]:
    """Acceptance text (one criterion per line, optional bullets) as a list."""
    lines = [ln.strip().lstrip("-*• ").strip() for ln in (acceptance or "").splitlines()]
    return [ln for ln in lines if ln]


@bp.route("/estimate/<engagement_id>")
def engagement_estimate(engagement_id: str):
    from app.models import Engagement
    eng = db.session.get(Engagement, engagement_id)
    if eng is None:
        abort(404)
    milestones = [{"idx": m.idx, "title": m.title, "criteria": _criteria(m.acceptance),
                   "amount_cents": (m.amount_micro or 0) // 10_000} for m in eng.milestones]
    est = estimate(outcome=eng.outcome,
                   milestones=[{"amount_cents": m["amount_cents"], "criteria": len(m["criteria"])}
                               for m in milestones],
                   category=eng.category or (eng.agent.category if eng.agent else None),
                   deadline=eng.deadline_at.date() if eng.deadline_at else None)
    return render_template("intake/estimate.html", eng=eng, milestones=milestones, est=est,
                           total_cents=(eng.total_micro or 0) // 10_000,
                           can_approve=eng.status in ("draft", "scoped"),
                           auto_release_days=AUTO_RELEASE_DAYS)


@bp.app_template_filter("usd")
def usd(cents: int | float | None) -> str:
    """Whole-cent amount as ``1,234.50``."""
    return f"{(cents or 0) / 100:,.2f}"
