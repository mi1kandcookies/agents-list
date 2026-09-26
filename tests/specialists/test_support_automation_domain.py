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


def test_tools_keep_inputs_read_only_and_kit_state_private(ws):
    original = (ws / "inputs" / "tickets.csv").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="read-only"):
        tools.redact_tickets(ws, output_path="inputs/tickets.csv")
    with pytest.raises(ValueError, match="internal"):
        tools.redact_tickets(ws, output_path=".agentkit/ledger.json")
    with pytest.raises(ValueError, match="internal"):
        tools.scan_pii(ws, path=".agentkit/ledger.json")
    assert (ws / "inputs" / "tickets.csv").read_text(encoding="utf-8") == original
    assert not (ws / ".agentkit").exists()


def test_tools_use_the_kit_resolver_when_given(ws):
    seen = []

    def resolve_path(p, *, write=False):
        seen.append((p, write))
        return tools._resolve(ws, p, write=write)

    tools.redact_tickets(ws, resolve_path=resolve_path)
    assert seen == [("inputs/tickets.csv", False), (f"{M1}/tickets_redacted.csv", True)]


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


# --- checks -------------------------------------------------------------------

from specialists.support_automation import checks as C  # noqa: E402

M1 = "deliverables/m1-discovery"
M2 = "deliverables/m2-knowledge"
M3 = "deliverables/m3-agent-config"
M1_PARAMS = {"tickets": f"{M1}/tickets_redacted.csv", "rules": f"{M1}/intent_rules.json",
             "taxonomy": f"{M1}/intent_taxonomy.csv", "kb_dir": "inputs/help_center"}


def _rewrite_csv(path: Path, mutate) -> None:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    mutate(rows)
    _write_csv(path, rows)


def test_pii_checks(ws):
    tools.redact_tickets(ws)
    assert C.no_pii_remaining(ws, {"paths": [M1]})["passed"] is True
    assert C.redaction_complete(ws, {"redacted": f"{M1}/tickets_redacted.csv"})["passed"] is True
    assert C.no_pii_remaining(ws, {"paths": ["inputs"]})["passed"] is False
    # forged: a dropped row fails even though no PII remains
    _rewrite_csv(ws / M1 / "tickets_redacted.csv", lambda rows: rows.pop())
    assert C.redaction_complete(ws, {"redacted": f"{M1}/tickets_redacted.csv"})["passed"] is False
    assert C.redaction_complete(ws, {"redacted": f"{M1}/missing.csv"})["passed"] is False


def test_taxonomy_reconciles_detects_forgery(ws):
    _m1(ws)
    ok = C.taxonomy_reconciles(ws, {**M1_PARAMS, "min_coverage": 0.8})
    assert ok["passed"] is True and ok["score"] == round(5 / 6, 4)
    assert C.taxonomy_reconciles(ws, {**M1_PARAMS, "min_coverage": 0.9})["passed"] is False

    def inflate(rows):
        rows[0]["volume"] = str(int(rows[0]["volume"]) + 3)
    _rewrite_csv(ws / M1 / "intent_taxonomy.csv", inflate)
    bad = C.taxonomy_reconciles(ws, {**M1_PARAMS, "min_coverage": 0.5})
    assert bad["passed"] is False and "reported" in bad["details"]


def test_gap_map_consistent_detects_forgery(ws):
    _m1(ws)
    params = {**M1_PARAMS, "gap_map": f"{M1}/kb_gap_map.csv"}
    assert C.gap_map_consistent(ws, params)["passed"] is True

    def hide_gaps(rows):
        for r in rows:
            r["status"] = "covered"
    _rewrite_csv(ws / M1 / "kb_gap_map.csv", hide_gaps)
    assert C.gap_map_consistent(ws, params)["passed"] is False


def _m2(ws: Path, *, source: str = "inputs/tickets.csv#T4") -> None:
    arts = ws / M2 / "articles"
    arts.mkdir(parents=True, exist_ok=True)
    (arts / "track-delivery.md").write_text(
        f"---\ntitle: Track your delivery\nintents: delivery_status\nsources: {source}\n---\n"
        "Deliveries arrive within 5 business days.\n", encoding="utf-8")
    (ws / M2 / "macros.json").write_text(json.dumps({"macros": [
        {"id": "m-delivery", "title": "Delivery delay", "intents": ["delivery_status"],
         "body": "Sorry for the wait - here is your tracking link.", "sources": [source]}]}), encoding="utf-8")


def test_m2_checks_pass_and_fail(ws):
    _m1(ws)
    _m2(ws)
    p = {**M1_PARAMS, "articles_dir": f"{M2}/articles", "macros": f"{M2}/macros.json", "top_n": 5}
    assert C.top_gaps_addressed(ws, p)["passed"] is True     # legal_threat is human_only, skipped
    assert C.articles_grounded(ws, {"articles_dir": f"{M2}/articles", "rules": p["rules"]})["passed"] is True
    assert C.macros_valid(ws, {"macros": p["macros"], "rules": p["rules"]})["passed"] is True
    assert C.kb_consistent(ws, {"terms": ["deliver"], "articles_dir": f"{M2}/articles",
                                "paths": [p["macros"]]})["passed"] is True
    # an article that contradicts the first on delivery time
    (ws / M2 / "articles" / "late.md").write_text(
        "---\nintents: delivery_status\nsources: inputs/tickets.csv#T99\n---\nDeliveries arrive within "
        "7 business days.\n", encoding="utf-8")
    assert C.kb_consistent(ws, {"terms": ["deliver"], "articles_dir": f"{M2}/articles"})["passed"] is False
    grounded = C.articles_grounded(ws, {"articles_dir": f"{M2}/articles"})
    assert grounded["passed"] is False and "T99" in grounded["details"]
    (ws / M2 / "macros.json").write_text(json.dumps([{"id": "x", "intents": ["nope"], "body": ""}]),
                                         encoding="utf-8")
    assert C.macros_valid(ws, {"macros": p["macros"], "rules": p["rules"]})["passed"] is False
    for f in (ws / M2 / "articles").glob("*.md"):
        f.unlink()
    miss = C.top_gaps_addressed(ws, {**p, "macros": None})
    assert miss["passed"] is False and "delivery_status" in miss["details"]


def _m3(ws: Path, rules: list[dict] | None = None) -> dict:
    cfg = {"instructions": "Be accurate; answer only from the knowledge base.",
           "ai_disclosure": "You are chatting with an AI assistant.",
           "handoff_message": "Connecting you with a teammate.",
           "knowledge_paths": ["inputs/help_center"],
           "escalation_rules": rules if rules is not None else [
               {"category": "billing_dispute", "keywords": ["chargeback", "dispute"]},
               {"category": "legal_threat", "keywords": ["attorney", "lawyer"]},
               {"category": "safety", "keywords": ["unsafe", "injury"]},
               {"category": "account_security", "keywords": ["hacked"]},
               {"category": "vulnerable_user", "keywords": ["bereavement"]}]}
    path = ws / M3 / "agent_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg


def test_agent_config_valid(ws):
    _m3(ws)
    assert C.agent_config_valid(ws, {"config": f"{M3}/agent_config.json"})["passed"] is True
    _m3(ws, rules=[{"category": "legal_threat", "keywords": ["attorney"]}])
    bad = C.agent_config_valid(ws, {"config": f"{M3}/agent_config.json"})
    assert bad["passed"] is False and "billing_dispute" in bad["details"]


def test_eval_holdout_sealed(ws):
    _m1(ws)
    tools.split_eval_set(ws, holdout_fraction=0.5)
    base = {"tickets": f"{M1}/tickets_labeled.csv", "holdout": f"{M3}/eval_holdout.csv",
            "build": f"{M3}/build_split.csv", "holdout_fraction": 0.5, "min_size": 1}
    assert C.eval_holdout_sealed(ws, base)["passed"] is True
    held = [r["ticket_id"] for r in csv.DictReader((ws / M3 / "eval_holdout.csv").open(encoding="utf-8"))]
    _m2(ws, source=f"inputs/tickets.csv#{held[0]}")        # leak a held-out ticket into the KB
    leak = C.eval_holdout_sealed(ws, {**base, "articles_dir": f"{M2}/articles"})
    assert leak["passed"] is False and held[0] in leak["details"]
    assert C.eval_holdout_sealed(ws, {**base, "holdout_fraction": 0.1})["passed"] is False


def test_escalation_recall_recomputes_and_rejects_forged_report(ws):
    _m1(ws)
    _m3(ws)
    params = {"config": f"{M3}/agent_config.json", "eval": f"{M1}/tickets_labeled.csv",
              "report": f"{M3}/escalation_replay.json"}
    tools.replay_escalations(ws, eval_path=params["eval"])
    assert C.escalation_recall(ws, params)["passed"] is True
    report = ws / params["report"]
    forged = json.loads(report.read_text(encoding="utf-8"))
    forged["false_neg"] = 0
    forged["must_escalate_recall"] = 1.0
    _m3(ws, rules=[{"category": "legal_threat", "keywords": ["attorney"]}])
    report.write_text(json.dumps(forged), encoding="utf-8")
    bad = C.escalation_recall(ws, params)
    assert bad["passed"] is False and bad["score"] == 0.5


def test_check_defs_are_callable():
    assert set(C.CHECK_DEFS) >= {"taxonomy_reconciles", "escalation_recall", "no_pii_remaining"}
    for fn in C.CHECK_DEFS.values():
        out = fn(Path("."), {}, run=None)
        assert set(out) == {"passed", "details", "score"} and out["passed"] is False


# --- manifest -----------------------------------------------------------------

import yaml  # noqa: E402

# Names from the kit contract (agentkit tools/ and checks/builtin.py).
KIT_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "search_files", "run_command",
             "http_fetch", "web_search", "read_document", "record_source", "record_claim",
             "ask_client", "post_progress", "submit_milestone"}
KIT_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
              "json_valid", "csv_columns", "command_succeeds", "ledger_verified", "citations_resolve",
              "disclaimer_present", "rubric_grader", "human_signoff"}


def _manifest() -> dict:
    return yaml.safe_load((PKG / "agent.yaml").read_text(encoding="utf-8"))


def test_manifest_parses_and_names_resolve():
    m = _manifest()
    assert m["schema_version"] == 1 and m["slug"] == "support-automation" and m["profile"] == "docs"
    domain_tools = {d["name"] for d in tools.TOOL_DEFS}
    assert set(m["tools"]) <= KIT_TOOLS | domain_tools
    assert domain_tools <= set(m["tools"])
    ids = [ms["id"] for ms in m["milestones"]]
    assert len(ids) == 3 == len(set(ids))
    for ms in m["milestones"]:
        assert all(d.startswith(f"deliverables/{ms['id']}/") for d in ms["deliverables"])
        for crit in ms["acceptance"]:
            assert crit["check"] in KIT_CHECKS | set(C.CHECK_DEFS), crit["check"]
            assert crit.get("kind", "automated") in ("automated", "rubric", "human")
    assert m["human_gate"]["required"] is False and m["human_gate"]["disclaimer"]
    assert m["egress"]["mode"] == "none" and m["shell"]["allow"] == []
    assert m["listing"]["pricing"]["currency"] == "USDC"


def test_manifest_files_and_rubrics_exist():
    m = _manifest()
    assert (PKG / m["prompts"]["system"]).is_file()
    for inc in m["prompts"]["include"]:
        assert (PKG / inc).is_file(), inc
    for ms in m["milestones"]:
        for crit in ms["acceptance"]:
            if crit["check"] == "rubric_grader":
                rubric = yaml.safe_load((PKG / crit["params"]["rubric"]).read_text(encoding="utf-8"))
                weights = [c["weight"] for c in rubric["criteria"]]
                assert rubric["name"] and 0 < rubric["threshold"] <= 1
                assert abs(sum(weights) - 1.0) < 1e-9
                assert all({"id", "description", "weight"} <= set(c) for c in rubric["criteria"])


# --- eval fixtures --------------------------------------------------------------

FIXTURE = PKG / "evals" / "fixtures" / "larkspur"


@pytest.fixture()
def larkspur(tmp_path: Path) -> Path:
    import shutil
    shutil.copytree(FIXTURE / "inputs", tmp_path / "inputs")
    (tmp_path / M1).mkdir(parents=True)
    shutil.copy(FIXTURE / "reference" / "intent_rules.json", tmp_path / M1 / "intent_rules.json")
    return tmp_path


def test_fixture_pipeline_meets_m1_acceptance(larkspur):
    red = tools.redact_tickets(larkspur)
    assert red["rows"] == 70 and all(red["redactions"].values())
    tools.build_intent_taxonomy(larkspur)
    gaps = dict(tools.kb_coverage(larkspur)["gaps"])
    assert {"pause_subscription", "cancel_subscription", "delivery_status"} <= set(gaps)
    assert "login_help" not in gaps and "skip_delivery" not in gaps
    assert C.taxonomy_reconciles(larkspur, M1_PARAMS)["passed"] is True
    assert C.gap_map_consistent(larkspur, {**M1_PARAMS, "gap_map": f"{M1}/kb_gap_map.csv"})["passed"] is True
    assert C.no_pii_remaining(larkspur, {"paths": [M1]})["passed"] is True
    found = tools.find_contradictions(larkspur, terms=["refund"], paths=["inputs/policies/refunds.md"],
                                      tickets_path="inputs/tickets.csv")
    refund = [c for c in found["conflicts"] if c["term"] == "refund" and c["unit"] == "days"]
    assert refund and {"14 days", "30 days"} <= set(refund[0]["values"])


def test_eval_cases_are_well_formed():
    m = _manifest()
    ids = {ms["id"] for ms in m["milestones"]}
    cases = sorted((PKG / "evals" / "cases").glob("*.json"))
    assert len(cases) >= 3
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert {"name", "brief", "milestone", "notes"} <= set(case)
        assert case["milestone"] in ids and case["brief"]["specialist"] == "support-automation"
        assert (PKG / case["fixtures"]).is_dir()
