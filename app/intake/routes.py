"""Routes for the intake blueprint."""
from __future__ import annotations

import io
import json

from flask import jsonify, render_template, request

from app.engagements.routes import _api
from app.engagements.service import EngagementError
from app.extensions import db, limiter
from app.intake import bp
from app.intake import sow_parse
from app.intake.estimate import (AUTO_RELEASE_DAYS, BUFFER, CATEGORIES, category_for, estimate,
                                 estimate_text)
from app.intake.token_model import model_config
from app.services import get_agent

MAX_PREFILL = 500


def flow_config() -> dict:
    """Everything flow.js needs that should not be duplicated in JavaScript."""
    return {"categories": CATEGORIES, "buffer": BUFFER,
            "auto_release_days": AUTO_RELEASE_DAYS, "token_model": model_config()}


@bp.route("/new")
def new_job():
    """The guided flow. ?q= prefills the outcome (home search box); ?agent=
    preselects an agent (the "Get estimate" link on an agent profile)."""
    prefill = (request.args.get("q") or "").strip()[:MAX_PREFILL]
    agent = get_agent(request.args.get("agent")) if request.args.get("agent") else None
    preselect = None
    # Only an agent that can be hired right now is preselected; the flow
    # would otherwise offer it and fail at approval.
    if agent and agent.get("operator_stamped"):
        cat = category_for(agent["category"])
        preselect = dict(agent, category_key=cat["key"] if cat else None)
    return render_template("intake/new.html", prefill=prefill, config=flow_config(),
                           agent=preselect, auto_release_days=AUTO_RELEASE_DAYS)


def _criteria(acceptance: str) -> list[str]:
    """Acceptance text (one criterion per line, optional bullets) as a list."""
    lines = [ln.strip().lstrip("-*• ").strip() for ln in (acceptance or "").splitlines()]
    return [ln for ln in lines if ln]


@bp.route("/estimate/<engagement_id>")
def engagement_estimate(engagement_id: str):
    from app.models import Engagement
    eng = db.session.get(Engagement, engagement_id)
    if eng is None:
        return render_template("404.html", missing=f"engagement {engagement_id}"), 404
    milestones = [{"idx": m.idx, "title": m.title, "criteria": _criteria(m.acceptance),
                   "amount_cents": (m.amount_micro or 0) // 10_000} for m in eng.milestones]
    est = estimate(outcome=eng.outcome,
                   milestones=[{"title": m["title"], "amount_cents": m["amount_cents"],
                                "criteria": m["criteria"]} for m in milestones],
                   category=eng.category or (eng.agent.category if eng.agent else None),
                   deadline=eng.deadline_at.date() if eng.deadline_at else None,
                   input_price_per_1m=eng.agent.input_price_per_1m if eng.agent else 0,
                   output_price_per_1m=eng.agent.output_price_per_1m if eng.agent else 0)
    from app.seller.stamp import stamp_status
    stamp = stamp_status(eng.agent) if eng.agent else None
    open_for_approval = eng.status in ("draft", "scoped")
    hireable = stamp is not None and stamp.ok
    return render_template("intake/estimate.html", eng=eng, milestones=milestones, est=est,
                           est_text=estimate_text(est),
                           total_cents=(eng.total_micro or 0) // 10_000,
                           can_approve=open_for_approval and hireable,
                           not_hireable=(stamp.reason if stamp else "This agent is no longer listed.")
                           if open_for_approval and not hireable else None,
                           source=json.loads(eng.sow_json or "{}").get("source_document"),
                           auto_release_days=AUTO_RELEASE_DAYS)


# ── SOW upload ────────────────────────────────────────────────────────────
@bp.route("/api/sow/parse", methods=["POST"])
@limiter.limit("30/minute")
@_api
def api_sow_parse():
    """Multipart ``file`` (.pdf/.docx/.txt/.md, up to 5 MB) or JSON
    ``{text, filename?}`` → a draft scope (app/intake/sow_parse.py). The file
    is read in memory only; nothing is stored."""
    try:
        if request.mimetype == "multipart/form-data":
            return jsonify(_parse_multipart())
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise sow_parse.SowParseError("Send a file (multipart field 'file') or JSON {text}.")
        text = body["text"]
        if len(text.encode("utf-8")) > sow_parse.MAX_BYTES:
            raise sow_parse.SowParseError("That text is larger than 5 MB.", "FILE_TOO_LARGE", 413)
        name = sow_parse.safe_filename(body.get("filename")) or None
        return jsonify(sow_parse.parse_text(text, filename=name))
    except sow_parse.SowParseError as exc:
        raise EngagementError(exc.message, exc.code, exc.status, "file") from None


def _parse_multipart() -> dict:
    limit = sow_parse.MAX_BYTES + 64 * 1024   # the file plus multipart overhead
    if (request.content_length or 0) > limit:
        raise sow_parse.SowParseError("That file is larger than 5 MB.", "FILE_TOO_LARGE", 413)
    request.max_content_length = limit
    # Keep uploads in memory: Werkzeug would otherwise spool large files to a
    # temporary file on disk.
    request._get_file_stream = lambda *a, **k: io.BytesIO()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise sow_parse.SowParseError("Choose a file to upload.")
    data = upload.read(sow_parse.MAX_BYTES + 1)
    return sow_parse.parse_upload(data, upload.filename)
