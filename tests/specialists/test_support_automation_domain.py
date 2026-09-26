"""
tests/specialists/test_support_automation_domain.py - unit tests for the
support-automation domain pack: tools, checks and the agent.yaml manifest.
All data is synthetic and built in tmp_path; nothing touches the network.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from specialists.support_automation import tools

PKG = Path(__file__).resolve().parents[2] / "specialists" / "support_automation"
# Built at runtime so no card-like literal sits in the repo.
CARD = "4111" + "1111" + "1111" + "1111"

TICKETS = [
    {"ticket_id": "T1", "subject": "Refund please", "body": f"Charge on card {CARD}, want my money back",
     "agent_reply": "Refunds are issued within 30 days.", "handle_minutes": "12", "status": "solved",
     "must_escalate": "0"},
    {"ticket_id": "T2", "subject": "Password reset", "body": "Cannot log in, email me at pat@example.com",
     "agent_reply": "Use the reset link.", "handle_minutes": "4", "status": "solved", "must_escalate": "0"},
    {"ticket_id": "T3", "subject": "Chargeback", "body": "I will dispute this refund with my bank",
     "agent_reply": "Refunds are issued within 14 days.", "handle_minutes": "30", "status": "escalated",
     "must_escalate": "1"},
    {"ticket_id": "T4", "subject": "Where is my box", "body": "Delivery late, call 555-201-3344",
     "agent_reply": "", "handle_minutes": "8", "status": "solved", "must_escalate": "0"},
    {"ticket_id": "T5", "subject": "Hello", "body": "Just saying thanks", "agent_reply": "",
     "handle_minutes": "1", "status": "solved", "must_escalate": "0"},
    {"ticket_id": "T6", "subject": "Lawyer", "body": "My attorney will contact you about this charge",
     "agent_reply": "", "handle_minutes": "20", "status": "escalated", "must_escalate": "1"},
]
RULES = {"intents": [
    {"id": "refund_request", "label": "Refund request", "keywords": ["refund", "money back"],
     "automation": "with_tools"},
    {"id": "login_help", "label": "Login help", "keywords": ["password", "log in"], "automation": "answer_only"},
    {"id": "delivery_status", "label": "Delivery status", "keywords": ["delivery", "where is my box"],
     "automation": "with_tools"},
    {"id": "legal_threat", "label": "Legal threat", "keywords": ["attorney", "lawyer"],
     "automation": "human_only"},
]}


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    _write_csv(tmp_path / "inputs" / "tickets.csv", TICKETS)
    kb = tmp_path / "inputs" / "help_center"
    kb.mkdir(parents=True)
    (kb / "reset-password.md").write_text("---\ntitle: Reset your password\nintents: login_help\n---\n"
                                          "Click forgot password to log in again.\n", encoding="utf-8")
    (kb / "refunds.md").write_text("# Refunds\nWant your money back? We issue a refund within 30 days of the request.\n",
                                   encoding="utf-8")
    m1 = tmp_path / "deliverables" / "m1-discovery"
    m1.mkdir(parents=True)
    (m1 / "intent_rules.json").write_text(json.dumps(RULES), encoding="utf-8")
    return tmp_path


def _m1(ws: Path) -> None:
    tools.redact_tickets(ws)
    tools.build_intent_taxonomy(ws)
    tools.kb_coverage(ws)


# --- tools --------------------------------------------------------------------

def test_luhn_and_find_pii():
    assert tools.luhn_ok(CARD)
    assert not tools.luhn_ok("4111111111111112")
    found = tools.find_pii(f"a@b.co {CARD} (555) 201-3344 order 20260926")
    assert found["email"] == ["a@b.co"] and len(found["card"]) == 1 and len(found["phone"]) == 1


def test_redact_tickets_removes_pii_and_keeps_rows(ws):
    out = tools.redact_tickets(ws)
    assert out["rows"] == len(TICKETS)
    assert out["redactions"] == {"email": 1, "phone": 1, "card": 1}
    scan = tools.scan_pii(ws, path=out["output_path"])
    assert scan["total"] == 0
    assert tools.scan_pii(ws, path="inputs/tickets.csv")["total"] == 3


def test_redact_refuses_path_escape(ws):
    with pytest.raises(ValueError):
        tools.redact_tickets(ws, input_path="../outside.csv")


def test_build_intent_taxonomy_counts(ws):
    tools.redact_tickets(ws)
    out = tools.build_intent_taxonomy(ws)
    assert out["tickets"] == 6
    assert out["coverage"] == round(5 / 6, 4)
    rows = list(csv.DictReader((ws / out["taxonomy_path"]).open(encoding="utf-8")))
    vol = {r["intent"]: int(r["volume"]) for r in rows}
    assert vol == {"refund_request": 2, "login_help": 1, "delivery_status": 1, "legal_threat": 1, "other": 1}
    assert rows[-1]["intent"] == "other"


def test_intent_rules_validation(ws):
    (ws / "bad.json").write_text(json.dumps({"intents": [{"id": "other", "keywords": ["x"]}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        tools.load_intent_rules(ws, "bad.json")
    (ws / "bad2.json").write_text(json.dumps({"intents": [{"id": "a", "keywords": ["x"], "automation": "magic"}]}),
                                  encoding="utf-8")
    with pytest.raises(ValueError):
        tools.load_intent_rules(ws, "bad2.json")


def test_kb_coverage_finds_gaps(ws):
    tools.redact_tickets(ws)
    tools.build_intent_taxonomy(ws)
    out = tools.kb_coverage(ws)
    gaps = dict(out["gaps"])
    assert "refund_request" not in gaps and "login_help" not in gaps
    assert set(gaps) == {"delivery_status", "legal_threat"}


def test_find_contradictions_flags_differing_days(ws):
    out = tools.find_contradictions(ws, terms=["refund"], tickets_path="inputs/tickets.csv")
    assert out["conflicts"], out
    conflict = out["conflicts"][0]
    assert conflict["term"] == "refund" and set(conflict["values"]) == {"30 days", "14 days"}
    none = tools.find_contradictions(ws, terms=["refund"])
    assert none["conflicts"] == []


def test_split_eval_set_is_deterministic_and_disjoint(ws):
    _m1(ws)
    a = tools.split_eval_set(ws, holdout_fraction=0.5)
    first = (ws / a["holdout_path"]).read_text(encoding="utf-8")
    b = tools.split_eval_set(ws, holdout_fraction=0.5)
    assert first == (ws / b["holdout_path"]).read_text(encoding="utf-8")
    assert a["holdout"] + a["build"] == len(TICKETS)
    with pytest.raises(ValueError):
        tools.split_eval_set(ws, holdout_fraction=1.5)


def test_replay_escalations_scores_recall(ws):
    _m1(ws)
    cfg = ws / "deliverables" / "m3-agent-config" / "agent_config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"escalation_rules": [
        {"category": "legal_threat", "keywords": ["attorney", "lawyer"]},
        {"category": "billing_dispute", "keywords": ["chargeback", "dispute"]}]}), encoding="utf-8")
    out = tools.replay_escalations(ws, eval_path="deliverables/m1-discovery/tickets_labeled.csv")
    assert out["must_escalate_recall"] == 1.0 and out["false_neg"] == 0
    cfg.write_text(json.dumps({"escalation_rules": [{"category": "legal_threat", "keywords": ["attorney"]}]}),
                   encoding="utf-8")
    weak = tools.replay_escalations(ws, eval_path="deliverables/m1-discovery/tickets_labeled.csv")
    assert weak["must_escalate_recall"] == 0.5 and weak["missed_ticket_ids"] == ["T3"]


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in tools.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in tools.TOOL_DEFS:
        assert callable(d["function"]) and d["description"] and d["input_schema"]["type"] == "object"
        assert d["risk"] in ("read", "write", "exec", "network", "external")
