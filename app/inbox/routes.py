"""/inbox: the signed-in buyer's delivered work."""
from __future__ import annotations

from pathlib import Path

from flask import abort, current_app, render_template, send_file

from app.extensions import db
from app.inbox import bp, service


def _human():
    from app.identity.session import current_human
    return current_human()


@bp.app_context_processor
def _nav_inbox():
    """``nav_inbox_count`` for the header (unread, available deliveries).
    Never breaks a page."""
    try:
        human = _human()
        return {"nav_inbox_count": service.unread_count(human) if human is not None else None}
    except Exception:  # noqa: BLE001
        return {"nav_inbox_count": None}


def _owned(delivery_id):
    """The signed-in buyer's available delivery, else 404."""
    from app.models import Delivery
    human = _human()
    row = db.session.get(Delivery, delivery_id)
    if (human is None or row is None or row.buyer_human_id != human.id
            or row.available_at > service.now()):
        abort(404)
    return row


@bp.get("/inbox")
def inbox():
    human = _human()
    if human is None:
        return render_template("inbox/list.html", deliveries=[], signed_out=True)
    return render_template("inbox/list.html", deliveries=service.for_buyer(human).all(),
                           signed_out=False)


@bp.get("/inbox/<delivery_id>")
def detail(delivery_id):
    row = _owned(delivery_id)
    if row.read_at is None:
        row.read_at = service.now()
        db.session.commit()
    att = row.attestation
    return render_template("inbox/detail.html", d=row, att=att,
                           att_hash=service.attestation_hash(att),
                           eng=row.engagement, agent=row.agent)


@bp.get("/inbox/<delivery_id>/report.pdf")
def download(delivery_id):
    row = _owned(delivery_id)
    path = Path(current_app.static_folder) / (row.pdf_path or "")
    if not row.pdf_path or not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                     download_name=path.name)
