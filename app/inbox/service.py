"""Deliveries for the buyer's Inbox.

When a job for the research demo agent is funded, a Delivery row is written
straight away with ``available_at = now + DELIVERY_DELAY_SECONDS``. Pages only
show deliveries whose ``available_at`` has passed, so the "agent is working"
period needs no background worker (the app runs serverless).

The dispatch to the agent is best effort: when LLM_URL (the orchestrator) and
FINANCE_LLM_URL (the finance agent) are set, one short chat completion each is
made with a 5 second timeout and the replies become the first log lines;
otherwise, or on any error, a plain log line is written instead. Funding never
waits on or fails because of it beyond that timeout.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.extensions import db

log = logging.getLogger(__name__)

DEMO_AGENT_NAME = "Ledgerline Financial Analyst"
PDF_PATH = "deliverables/northwind-equity-research.pdf"   # under app/static
REPORT_TITLE = "Equity research: Northwind Robotics, Inc. (NWRB)"
REPORT_SUMMARY = (
    "Initiation report on Northwind Robotics with a three-year financial analysis, "
    "a DCF and comparables valuation, key risks and an Outperform recommendation "
    "with a $58 price target."
)
DISPATCH_TIMEOUT = 5
LOG_TRUNCATE = 280


def now() -> datetime:
    """Naive UTC now (the column is naive on SQLite and Postgres alike).
    Tests patch this."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def delay_seconds() -> int:
    try:
        return max(0, int(os.environ.get("DELIVERY_DELAY_SECONDS", "10")))
    except ValueError:
        return 10


def delivers(agent) -> bool:
    return agent is not None and agent.name == DEMO_AGENT_NAME


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def attestation_hash(payload: dict) -> str:
    """sha256 of the canonical JSON of the attestation payload, as hex."""
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _pdf_sha256() -> str | None:
    path = Path(__file__).resolve().parent.parent / "static" / PDF_PATH
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def _dispatch_lines(eng, agent) -> list[str]:
    """Best-effort dispatch over the orchestrator and finance endpoints.
    Always returns log lines; never raises."""
    from app import llm
    sow_ref = (eng.sow_hash or "")[:18]
    brief = (f"SOW {eng.id} (hash {sow_ref}): {eng.outcome[:400]}. Milestones: "
             + "; ".join(f"{m.idx + 1}. {m.title}" for m in eng.milestones))
    lines = []
    orch = llm._default_endpoint()
    reply = None
    if orch.configured:
        try:
            body = {"model": orch.model, "max_tokens": 120, "temperature": 0.2, "messages": [
                {"role": "system", "content": (
                    "You are the Agent's List orchestrator. You hand a funded statement of "
                    "work to a specialist agent. Reply in two short sentences: confirm the "
                    "handoff and name the first step.")},
                {"role": "user", "content": f"Hand this to {agent.name}. {brief}"}]}
            payload = llm._post(orch, body, timeout=DISPATCH_TIMEOUT)
            reply = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception as exc:  # noqa: BLE001
            log.info("orchestrator dispatch for %s skipped: %s", eng.id, str(exc)[:120])
    if reply:
        lines.append(f"Agent's List orchestrator on Akash dispatched SOW {eng.id} to "
                     f"{agent.name}: {' '.join(reply.split())[:LOG_TRUNCATE]}")
    else:
        lines.append(f"Agent's List orchestrator on Akash dispatched SOW {eng.id} "
                     f"(hash {sow_ref}) to {agent.name}")
    fin = llm._finance_endpoint()
    ack = None
    if fin.configured:
        try:
            body = {"model": fin.model, "max_tokens": 120, "temperature": 0.2, "messages": [
                {"role": "system", "content": (
                    f"You are {agent.name}, an equity research agent. Acknowledge the "
                    "statement of work in one or two sentences and state your plan.")},
                {"role": "user", "content": brief}]}
            payload = llm._post(fin, body, timeout=DISPATCH_TIMEOUT)
            ack = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception as exc:  # noqa: BLE001
            log.info("finance agent dispatch for %s skipped: %s", eng.id, str(exc)[:120])
    if ack:
        lines.append(f"{agent.name} acknowledged: {' '.join(ack.split())[:LOG_TRUNCATE]}")
    else:
        lines.append(f"{agent.name} accepted SOW {eng.id}; plan: filings, model, "
                     "valuation, report")
    return lines


def schedule(eng) -> "Delivery | None":
    """Write the delivery for a just-funded job of the demo agent. Idempotent
    per engagement. Returns the row, or None when the agent does not deliver
    this way."""
    from app.models import Delivery
    agent = eng.agent
    if not delivers(agent) or eng.parent_engagement_id is not None:
        return None
    existing = Delivery.query.filter_by(engagement_id=eng.id).first()
    if existing is not None:
        return existing
    start = now()
    done = start + timedelta(seconds=delay_seconds())
    milestones = [{"idx": m.idx, "title": m.title, "acceptance": m.acceptance, "status": "met"}
                  for m in eng.milestones]
    attestation = {
        "type": "sow-completion-attestation",
        "version": 1,
        "engagement_id": eng.id,
        "sow_hash": eng.sow_hash,
        "agent_id": agent.public_id,
        "agent_name": agent.name,
        "completed_at": _iso(done),
        "milestones": [{"idx": m["idx"], "title": m["title"], "status": "met"}
                       for m in milestones],
        "all_milestones_met": True,
        "deliverable": {"path": PDF_PATH, "sha256": _pdf_sha256()},
    }
    span = max(1, delay_seconds())

    def at(frac: float) -> str:
        return _iso(start + timedelta(seconds=span * frac))

    steps = [(0.00, "info", line) for line in _dispatch_lines(eng, agent)]
    steps += [
        (0.08, "info", f"Fetched SOW {eng.id} and verified hash {(eng.sow_hash or '')[:18]}"),
        (0.14, "info", "Loaded tools: sec_filings, spreadsheet, pdf_writer"),
        (0.24, "info", "Gathered filings: 10-K FY2023 to FY2025, three 10-Q, proxy statement"),
        (0.38, "info", "Built model: three-year P&L, cash flow and working capital"),
        (0.52, "info", "Valuation: DCF (WACC 9.4%, g 3.0%) and 6-company comps set"),
        (0.66, "info", "Drafted report: 6 pages, 6 tables, recommendation Outperform"),
        (0.76, "info", "Ran checks: totals tie out, sources cited, no unverified figures"),
    ]
    for i, m in enumerate(milestones):
        steps.append((0.80 + 0.02 * i, "info", f"Milestone {m['idx'] + 1} \"{m['title']}\" met"))
    steps += [
        (0.92, "info", f"Uploaded deliverable {PDF_PATH.rsplit('/', 1)[-1]}"),
        (1.00, "info", f"Attestation signed sha256:{attestation_hash(attestation)[:16]}"),
    ]
    logs = [{"ts": at(f), "level": lvl, "msg": msg} for f, lvl, msg in steps]
    row = Delivery(engagement_id=eng.id, agent_id=agent.id, buyer_human_id=eng.buyer_human_id,
                   title=REPORT_TITLE, summary=REPORT_SUMMARY,
                   attestation_json=canonical(attestation), logs_json=json.dumps(logs),
                   milestones_json=json.dumps(milestones), pdf_path=PDF_PATH,
                   available_at=done, created_at=start)
    db.session.add(row)
    db.session.commit()
    return row


def on_engagement_funded(eng) -> None:
    """After-fund hook. Never raises."""
    try:
        schedule(eng)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log.warning("delivery for %s not scheduled: %s", eng.id, str(exc)[:200])


def visible(query):
    from app.models import Delivery
    return query.filter(Delivery.available_at <= now())


def for_buyer(human):
    from app.models import Delivery
    return visible(Delivery.query.filter(Delivery.buyer_human_id == human.id)) \
        .order_by(Delivery.available_at.desc())


def unread_count(human) -> int:
    from app.models import Delivery
    return visible(Delivery.query.filter(Delivery.buyer_human_id == human.id,
                                         Delivery.read_at.is_(None))).count()


def for_engagement(eng) -> tuple["Delivery | None", bool]:
    """(delivery, available) for the job page."""
    from app.models import Delivery
    row = Delivery.query.filter_by(engagement_id=eng.id).first()
    if row is None:
        return None, False
    return row, row.available_at <= now()
