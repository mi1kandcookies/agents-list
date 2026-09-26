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
    pol = tmp_path / "inputs" / "policies"
    pol.mkdir()
    (pol / "shipping.md").write_text("# Shipping\nDeliveries arrive within 5 business days\nof shipping.\n",
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
M1_PARAMS = {"source": "inputs/tickets.csv", "rules": f"{M1}/intent_rules.json",
             "taxonomy": f"{M1}/intent_taxonomy.csv", "kb_dir": "inputs/help_center"}
POLICY = "inputs/policies/shipping.md"


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
        f"---\ntitle: Track your delivery\nintents: delivery_status\nsources: {POLICY}, {source}\n---\n"
        "Deliveries arrive within 5 business days.\n", encoding="utf-8")
    (ws / M2 / "macros.json").write_text(json.dumps({"macros": [
        {"id": "m-delivery", "title": "Delivery delay", "intents": ["delivery_status"],
         "body": "Sorry for the wait - here is your tracking link.", "sources": [POLICY, source]}]}),
        encoding="utf-8")


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
    # a file written during the engagement is not client material, even via inputs/..
    for src in (f"{M1}/intent_rules.json", f"inputs/../{M1}/intent_rules.json"):
        (ws / M2 / "articles" / "late.md").write_text(
            f"---\nintents: delivery_status\nsources: {src}\n---\nDeliveries arrive within 5 business days.\n",
            encoding="utf-8")
        own = C.articles_grounded(ws, {"articles_dir": f"{M2}/articles"})
        assert own["passed"] is False and "late.md: source not found under inputs/" in own["details"]
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
    base = {"tickets": f"{M1}/tickets_labeled.csv", "holdout": f"{M1}/eval_holdout.csv",
            "build": f"{M1}/build_split.csv", "holdout_fraction": 0.5, "min_size": 1}
    assert C.eval_holdout_sealed(ws, base)["passed"] is True
    held = [r["ticket_id"] for r in csv.DictReader((ws / M1 / "eval_holdout.csv").open(encoding="utf-8"))]
    _m2(ws, source=f"inputs/tickets.csv#{held[0]}")        # leak a held-out ticket into the KB
    leak = C.eval_holdout_sealed(ws, {**base, "articles_dir": f"{M2}/articles"})
    assert leak["passed"] is False and held[0] in leak["details"]
    assert C.eval_holdout_sealed(ws, {**base, "holdout_fraction": 0.1})["passed"] is False


def test_eval_holdout_sealed_against_source_export(ws):
    _m1(ws)
    tools.split_eval_set(ws, holdout_fraction=0.5)
    params = {"tickets": f"{M1}/tickets_labeled.csv", "source": "inputs/tickets.csv",
              "holdout": f"{M1}/eval_holdout.csv", "build": f"{M1}/build_split.csv",
              "holdout_fraction": 0.5, "min_size": 1}
    assert C.eval_holdout_sealed(ws, params)["passed"] is True
    # relabel a held-out must-escalate ticket so a weak rule set looks perfect
    hold = ws / M1 / "eval_holdout.csv"
    rows = list(csv.DictReader(hold.open(encoding="utf-8")))
    target = next((r for r in rows if r["must_escalate"] == "1"), rows[0])
    target["must_escalate"] = "0" if target["must_escalate"] == "1" else "1"
    _write_csv(hold, rows)
    bad = C.eval_holdout_sealed(ws, params)
    assert bad["passed"] is False and target["ticket_id"] in bad["details"]
    # dropping a ticket from both the labeled export and the split is caught too
    tools.split_eval_set(ws, holdout_fraction=0.5)
    _rewrite_csv(ws / M1 / "tickets_labeled.csv", lambda rows: rows.pop())
    tools.split_eval_set(ws, tickets_path=f"{M1}/tickets_labeled.csv", holdout_fraction=0.5)
    dropped = C.eval_holdout_sealed(ws, params)
    assert dropped["passed"] is False and "inputs/tickets.csv has 6" in dropped["details"]


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
    assert red["rows"] == 72 and all(red["redactions"].values())
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
    # the one-off promise in an agent reply is flagged for the policy owner, not taken as policy
    assert refund[0]["values"]["21 days"] == ["inputs/tickets.csv#LP0072"]
    # a wrapped policy line still yields its limit; the $20 credit is not a refund amount
    assert tools.policy_numbers((larkspur / "inputs/policies/refunds.md").read_text(encoding="utf-8"),
                                ["refund", "credit"]) == {"refund": {"14 days", "$60"}, "credit": {"$20"}}


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


# --- review regressions ----------------------------------------------------------

def _escalation_config(ws: Path, rules: list[dict]) -> str:
    _m3(ws, rules=rules)
    return f"{M3}/agent_config.json"


def test_malformed_or_catch_all_escalation_rules_fail(ws):
    _m1(ws)
    base = [{"category": c, "keywords": [k]} for c, k in (
        ("billing_dispute", "chargeback"), ("legal_threat", "attorney"), ("safety", "unsafe"),
        ("account_security", "hacked"))]
    recall = {"eval": f"{M1}/tickets_labeled.csv"}
    # a bare string is not a keyword list: it must not match every ticket through its letters
    cfg = _escalation_config(ws, base + [{"category": "vulnerable_user", "keywords": "bereavement"}])
    bad = C.agent_config_valid(ws, {"config": cfg})
    assert bad["passed"] is False and "vulnerable_user" in bad["details"]
    assert tools.escalation_decision("hello there", [{"category": "x", "keywords": "bereavement"}]) is None
    # an empty keyword would escalate everything
    catch_all = base + [{"category": "vulnerable_user", "keywords": ["bereavement"]},
                        {"category": "catch_all", "keywords": [""]}]
    cfg = _escalation_config(ws, catch_all)
    assert C.agent_config_valid(ws, {"config": cfg})["passed"] is False
    out = C.escalation_recall(ws, {"config": cfg, **recall})
    assert out["passed"] is False and "malformed rules" in out["details"]
    assert tools.escalation_decision("thanks!", catch_all) is None
    # keywords match at the start of a word
    rule = [{"category": "legal_threat", "keywords": ["sue"]}]
    assert tools.escalation_decision("an issue with my tissue order", rule) is None
    assert tools.escalation_decision("I will sue you", rule) == "legal_threat"
    assert tools.escalation_decision("They sued us", rule) == "legal_threat"


def test_intent_rules_reject_catch_all_keywords(ws):
    for keywords in ("refund", [""], ["ab"], [3]):
        bad = {"intents": [{"id": "general", "keywords": keywords}]}
        (ws / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError, match="keyword"):
            tools.load_intent_rules(ws, "bad.json")


def test_escalating_everything_fails_the_precision_floor(tmp_path):
    rows = [{"ticket_id": f"P{i}", "subject": "Help please", "body": "please look at my order",
             "must_escalate": "1" if i == 0 else "0"} for i in range(10)]
    _write_csv(tmp_path / "eval.csv", rows)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"escalation_rules": [{"category": "everything", "keywords": ["please"]}]}),
                   encoding="utf-8")
    out = C.escalation_recall(tmp_path, {"config": "cfg.json", "eval": "eval.csv"})
    assert out["passed"] is False and "precision 0.10 below 0.30" in out["details"]
    assert "9 of 9 ordinary tickets" in out["details"]
    # the same rule set passes when told to ignore precision, so the floor is what failed it
    assert C.escalation_recall(tmp_path, {"config": "cfg.json", "eval": "eval.csv", "min_precision": 0})["passed"]


def test_escalation_recall_needs_enough_positives(ws):
    _m1(ws)
    _m3(ws)
    params = {"config": f"{M3}/agent_config.json", "eval": f"{M1}/tickets_labeled.csv"}
    ok = C.escalation_recall(ws, params)
    assert ok["passed"] is True and "(2/2, 95% lower bound 0.34)" in ok["details"]
    few = C.escalation_recall(ws, {**params, "min_positives": 3})
    assert few["passed"] is False and "2 must-escalate tickets, need at least 3" in few["details"]
    assert tools.recall_lower_bound(20, 20) == pytest.approx(0.8389, abs=1e-4)


def test_anchorless_ticket_source_is_rejected_and_counts_as_leakage(ws):
    _m1(ws)
    tools.split_eval_set(ws, holdout_fraction=0.5)
    _m2(ws, source="inputs/tickets.csv")
    grounded = C.articles_grounded(ws, {"articles_dir": f"{M2}/articles"})
    assert grounded["passed"] is False and "not the whole export" in grounded["details"]
    macros = C.macros_valid(ws, {"macros": f"{M2}/macros.json", "rules": f"{M1}/intent_rules.json"})
    assert macros["passed"] is False and "not the whole export" in macros["details"]
    held = [r["ticket_id"] for r in csv.DictReader((ws / M1 / "eval_holdout.csv").open(encoding="utf-8"))]
    leak = C.eval_holdout_sealed(ws, {"source": "inputs/tickets.csv", "holdout": f"{M1}/eval_holdout.csv",
                                      "build": f"{M1}/build_split.csv", "holdout_fraction": 0.5,
                                      "articles_dir": f"{M2}/articles"})
    assert leak["passed"] is False and all(t in leak["details"] for t in held)


def test_top_gaps_use_export_volumes_and_fail_when_every_gap_is_human_only(ws):
    _m1(ws)
    _m2(ws)
    p = {**M1_PARAMS, "articles_dir": f"{M2}/articles", "macros": f"{M2}/macros.json", "top_n": 5}
    assert C.top_gaps_addressed(ws, p)["passed"] is True

    # zeroing a gap's volume in the taxonomy changes nothing: volumes come from the export
    def zero(rows):
        for r in rows:
            r["volume"] = "0"
    _rewrite_csv(ws / M1 / "intent_taxonomy.csv", zero)
    (ws / M2 / "articles" / "track-delivery.md").unlink()
    miss = C.top_gaps_addressed(ws, {**p, "macros": None})
    assert miss["passed"] is False and "delivery_status" in miss["details"]
    # marking every gap human_only leaves nothing to fill, which fails
    rules = json.loads(json.dumps(RULES))
    for intent in rules["intents"]:
        intent["automation"] = "human_only"
    (ws / M1 / "intent_rules.json").write_text(json.dumps(rules), encoding="utf-8")
    flipped = C.top_gaps_addressed(ws, p)
    assert flipped["passed"] is False and "human_only" in flipped["details"]


def _submit(ws: Path, milestone: str, paths: list[str]) -> None:
    import hashlib
    arts = [{"path": rel, "sha256": hashlib.sha256((ws / rel).read_bytes()).hexdigest()} for rel in paths]
    sub = ws / ".agentkit" / "submissions" / f"{milestone}.json"
    sub.parent.mkdir(parents=True, exist_ok=True)
    sub.write_text(json.dumps({"status": "ready_for_review", "artifacts": arts}), encoding="utf-8")


def test_prior_milestone_unchanged_pins_submitted_files(ws):
    _m1(ws)
    _m2(ws)
    none = C.prior_milestone_unchanged(ws, {"milestone": "m1-discovery"})
    assert none["passed"] is True and "nothing to pin" in none["details"]
    _submit(ws, "m1-discovery", [f"{M1}/intent_rules.json", f"{M1}/intent_taxonomy.csv"])
    _submit(ws, "m2-knowledge", [f"{M2}/articles/track-delivery.md", f"{M2}/macros.json"])
    assert C.prior_milestone_unchanged(ws, {"milestone": "m1-discovery"})["passed"] is True
    pin2 = {"milestone": "m2-knowledge", "dirs": [f"{M2}/articles"]}
    assert C.prior_milestone_unchanged(ws, pin2)["passed"] is True
    rules = json.loads(json.dumps(RULES))
    rules["intents"][2]["automation"] = "human_only"
    (ws / M1 / "intent_rules.json").write_text(json.dumps(rules), encoding="utf-8")
    changed = C.prior_milestone_unchanged(ws, {"milestone": "m1-discovery"})
    assert changed["passed"] is False and f"{M1}/intent_rules.json" in changed["details"]
    (ws / M2 / "articles" / "extra.md").write_text("# Extra\n", encoding="utf-8")
    added = C.prior_milestone_unchanged(ws, pin2)
    assert added["passed"] is False and f"{M2}/articles/extra.md (added)" in added["details"]


def test_redaction_and_reconciliation_reject_rewritten_text_and_forged_fields(ws):
    _m1(ws)
    params = {**M1_PARAMS, "labeled": f"{M1}/tickets_labeled.csv", "min_coverage": 0.8}
    assert C.taxonomy_reconciles(ws, params)["passed"] is True
    assert C.redaction_complete(ws, {"redacted": f"{M1}/tickets_redacted.csv"})["passed"] is True

    # appending a keyword to an unclassified ticket would inflate coverage; the copy must match the export
    def append(rows):
        rows[4]["body"] += " refund"
    _rewrite_csv(ws / M1 / "tickets_redacted.csv", append)
    red = C.redaction_complete(ws, {"redacted": f"{M1}/tickets_redacted.csv"})
    assert red["passed"] is False and "rows differ from inputs/tickets.csv: T5" in red["details"]
    # the taxonomy re-derives from the export, not from the rewritten copy
    tools.build_intent_taxonomy(ws)
    assert C.taxonomy_reconciles(ws, params)["passed"] is False
    tools.redact_tickets(ws)
    tools.build_intent_taxonomy(ws)

    # every field of a taxonomy row is compared, not only the volume
    def forge(rows):
        rows[0]["avg_handle_minutes"], rows[0]["escalation_rate"], rows[0]["share"] = "99.9", "0.0", "0.5"
    _rewrite_csv(ws / M1 / "intent_taxonomy.csv", forge)
    bad = C.taxonomy_reconciles(ws, params)
    assert bad["passed"] is False and "avg_handle_minutes 99.9" in bad["details"] and "share 0.5" in bad["details"]
    tools.build_intent_taxonomy(ws)

    def relabel(rows):
        rows[4]["intent"] = "login_help"
    _rewrite_csv(ws / M1 / "tickets_labeled.csv", relabel)
    assert "intent differs from the rules for T5" in C.taxonomy_reconciles(ws, params)["details"]
    tools.build_intent_taxonomy(ws)

    # a bogus extra row cannot hide behind the correct one for the same intent
    def duplicate(rows):
        rows.insert(0, {**rows[0], "volume": "50"})
    _rewrite_csv(ws / M1 / "intent_taxonomy.csv", duplicate)
    assert "repeated rows for refund_request" in C.taxonomy_reconciles(ws, params)["details"]

    # gap map: volume and covering articles are compared too
    def gap_forge(rows):
        for r in rows:
            r["volume"] = "0" if r["intent"] == "delivery_status" else r["volume"]
            r["articles"] = "made-up.md" if r["intent"] == "login_help" else r["articles"]
    _rewrite_csv(ws / M1 / "kb_gap_map.csv", gap_forge)
    out = C.gap_map_consistent(ws, {**M1_PARAMS, "gap_map": f"{M1}/kb_gap_map.csv"})
    assert out["passed"] is False and "delivery_status: volume 0 vs 1" in out["details"]
    assert "login_help: articles made-up.md vs reset-password.md" in out["details"]


def test_ticket_export_valid(ws):
    assert C.ticket_export_valid(ws, {})["passed"] is True
    no_label = [{k: v for k, v in t.items() if k != "must_escalate"} for t in TICKETS]
    _write_csv(ws / "inputs" / "tickets.csv", no_label)
    out = C.ticket_export_valid(ws, {})
    assert out["passed"] is False and "lacks column(s) must_escalate" in out["details"]
    rows = [dict(t) for t in TICKETS]
    rows[1]["must_escalate"] = ""
    rows[2]["ticket_id"] = "T1"
    _write_csv(ws / "inputs" / "tickets.csv", rows)
    out = C.ticket_export_valid(ws, {})
    assert "not 1/0 on 1 tickets: T2" in out["details"] and "duplicate ticket_ids: T1" in out["details"]
    _write_csv(ws / "inputs" / "tickets.csv", [{**t, "must_escalate": "0"} for t in TICKETS])
    assert "no ticket has must_escalate=1" in C.ticket_export_valid(ws, {})["details"]


def test_excel_bom_and_padded_headers_are_read(ws):
    src = ws / "inputs" / "tickets.csv"
    text = src.read_text(encoding="utf-8").replace("subject,body", "subject, body ", 1)
    src.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert C.ticket_export_valid(ws, {})["passed"] is True
    tools.redact_tickets(ws)
    assert C.redaction_complete(ws, {"redacted": f"{M1}/tickets_redacted.csv"})["passed"] is True
    tools.split_eval_set(ws, holdout_fraction=0.5)
    sealed = C.eval_holdout_sealed(ws, {"source": "inputs/tickets.csv", "holdout": f"{M1}/eval_holdout.csv",
                                        "build": f"{M1}/build_split.csv", "holdout_fraction": 0.5})
    assert sealed["passed"] is True, sealed
    # an export without ticket ids fails with a reason instead of passing on None == None
    src.write_text("id,subject,body\n1,a,b\n", encoding="utf-8")
    out = C.redaction_complete(ws, {"redacted": f"{M1}/tickets_redacted.csv"})
    assert out["passed"] is False and "no ticket_id column" in out["details"]


def test_articles_folder_holds_only_grounded_markdown(ws):
    from agentkit.checks import CheckContext
    from specialists.support_automation.agent import no_placeholders_dirs

    _m1(ws)
    _m2(ws)
    arts = ws / M2 / "articles"
    params = {"articles_dir": f"{M2}/articles", "rules": f"{M1}/intent_rules.json"}
    assert C.articles_grounded(ws, params)["passed"] is True
    (arts / "extra.txt").write_text("Refunds within 90 days, always, no questions asked.\n", encoding="utf-8")
    (arts / "nested").mkdir()
    out = C.articles_grounded(ws, params)
    assert out["passed"] is False and "extra.txt: only Markdown" in out["details"]
    assert "nested: only Markdown" in out["details"]
    (arts / "extra.txt").unlink()
    (arts / "nested").rmdir()
    # placeholder text inside the articles folder is found through the directory
    (arts / "todo.md").write_text(f"---\nintents: delivery_status\nsources: {POLICY}\n---\nShip within TBD days.\n",
                                  encoding="utf-8")
    ph = no_placeholders_dirs(ws, {"paths": [f"{M2}/articles"]}, CheckContext())
    assert ph.passed is False and "todo.md:5" in ph.details
    (arts / "todo.md").unlink()
    assert no_placeholders_dirs(ws, {"paths": [f"{M2}/articles"]}, CheckContext()).passed is True
    # macros keep {{customer}} fields but not placeholder text
    macros = json.loads((ws / M2 / "macros.json").read_text(encoding="utf-8"))
    macros["macros"][0]["body"] = "Hi {{first_name}}, [insert refund rule]."
    (ws / M2 / "macros.json").write_text(json.dumps(macros), encoding="utf-8")
    bad = C.macros_valid(ws, {"macros": f"{M2}/macros.json", "rules": f"{M1}/intent_rules.json"})
    assert bad["passed"] is False and "placeholder text" in bad["details"]


def test_policy_numbers_join_wrapped_lines_and_read_n_day_forms():
    assert tools.policy_numbers("Request a refund within\n30 days of delivery.", ["refund"]) == {"refund": {"30 days"}}
    assert tools.policy_numbers("We offer a 30-day refund window.", ["refund"]) == {"refund": {"30 days"}}
    assert tools.policy_numbers("# Refunds\nWe ship in 2 business days.", ["refund"]) == {"refund": set()}
    assert tools.policy_values("$ 60.00 or 10 % within 1 week") == {"$60", "10%", "1 weeks"}


def test_phone_numbers_in_common_formats_are_found():
    text = "Call +44 20 7946 0958, 07700 900123, 5550142231 or (555) 201-3344. Order 20260926 on 2026-03-18."
    assert tools.find_pii(text)["phone"] == ["+44 20 7946 0958", "07700 900123", "5550142231", "(555) 201-3344"]
    redacted, counts = tools.redact_text(text)
    assert counts["phone"] == 4 and "20260926" in redacted and "2026-03-18" in redacted


def test_redaction_covers_every_column_but_the_id(ws):
    rows = [dict(t, requester_email=f"user{i}@example.com") for i, t in enumerate(TICKETS)]
    _write_csv(ws / "inputs" / "tickets.csv", rows)
    out = tools.redact_tickets(ws)
    assert "requester_email" in out["columns"] and "ticket_id" not in out["columns"]
    assert tools.scan_pii(ws, path=out["output_path"])["total"] == 0


def test_milestone_estimates_fit_the_run_limits():
    m = _manifest()
    rate, limits = m["estimate"]["usd_per_hour"], m["limits"]
    for ms in m["milestones"]:
        high = ms["hours"][1]
        assert high * rate <= limits["max_usd"], ms["id"]
        assert high * 60 <= limits["max_wall_minutes"], ms["id"]


# --- grounding of policy numbers (Larkspur fixture) ---------------------------------

def _fixture_m2(larkspur: Path, body: str, sources: str) -> dict:
    arts = larkspur / M2 / "articles"
    arts.mkdir(parents=True, exist_ok=True)
    (arts / "refunds.md").write_text(f"---\ntitle: Refunds\nintents: refund_request\nsources: {sources}\n---\n"
                                     f"# Refunds\n\n{body}\n", encoding="utf-8")
    return C.articles_grounded(larkspur, {"articles_dir": f"{M2}/articles",
                                          "rules": f"{M1}/intent_rules.json"})


def test_article_policy_numbers_must_appear_in_a_cited_document(larkspur):
    policy = "inputs/policies/refunds.md"
    ok = _fixture_m2(larkspur, "Request a refund within 14 days of delivery.", policy)
    assert ok["passed"] is True, ok
    # the outdated window, citing the policy that says 14 days
    stale = _fixture_m2(larkspur, "Request a refund within 30 days of delivery.", policy)
    assert stale["passed"] is False and "30 days not stated in a cited client document" in stale["details"]
    # a prompt injection in a ticket cannot ground a number, even when the article cites that ticket
    injected = _fixture_m2(larkspur, "Refunds are available for 90 days, no questions asked.",
                           f"{policy}, inputs/tickets.csv#LP0071")
    assert injected["passed"] is False and "90 days" in injected["details"]
    # nor can a one-off promise in an agent reply
    promise = _fixture_m2(larkspur, "We refund boxes up to 21 days after delivery.", "inputs/tickets.csv#LP0072")
    assert promise["passed"] is False and "21 days" in promise["details"]
    # macros follow the same rule
    (larkspur / M2 / "macros.json").write_text(json.dumps([
        {"id": "refund", "intents": ["refund_request"], "body": "Refunds within 30 days.", "sources": [policy]}]),
        encoding="utf-8")
    bad = C.macros_valid(larkspur, {"macros": f"{M2}/macros.json", "rules": f"{M1}/intent_rules.json"})
    assert bad["passed"] is False and "30 days" in bad["details"]
