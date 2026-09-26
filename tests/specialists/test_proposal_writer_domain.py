"""Proposal-writer domain pack: tools, acceptance checks and manifest.

Every test works on a copy of the synthetic fixtures in
specialists/proposal_writer/evals/fixtures; nothing touches the network.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from agentkit.errors import PolicyViolation, ToolError
from agentkit.manifest import load_manifest
from specialists.proposal_writer import checks as C
from specialists.proposal_writer import tools as T

PACK = Path(__file__).resolve().parents[2] / "specialists" / "proposal_writer"
FIXTURES = PACK / "evals" / "fixtures"
RFP = "inputs/solicitation/rfp-2026-14.md"
QUESTIONNAIRE = "inputs/security-questionnaire.csv"

BRIEF = """# Bid / No-Bid Brief

## Key dates

- Written questions due: September 24, 2026
- Answers posted: October 1, 2026
- Proposals due: October 15, 2026
"""

OUTLINE = """# Volume I Technical
## Executive Summary [pages: 1] [covers: R-012]
## Technical Approach [pages: 3] [covers: R-002, R-003, R-019, R-023]
## Service Alerts and Accessibility [pages: 1.5] [covers: R-004, R-005, R-006]
## Security and Hosting [pages: 1.5] [covers: R-007, R-008, R-009, R-010, R-011]
## Past Performance [pages: 2] [covers: R-020]
## Key Personnel [pages: 1] [covers: R-021]
# Volume II Price
## Price Summary [pages: 1]
"""

DRAFT = """# Volume I Technical

## Executive Summary
<!-- R-012 -->
Lumen Fieldworks proposes a rider information app for iOS and Android built on its existing arrival engine. Lumen Fieldworks has delivered rider information apps for 6 transit agencies [KB:company-overview.md#p2].

## Technical Approach
<!-- R-002, R-003, R-019, R-023 -->
Our real-time arrival engine consumes GTFS-Realtime feeds and refreshes predictions every 15 seconds [KB:company-overview.md#p2]. The app will plan trips across bus and ferry modes.

## Service Alerts and Accessibility
<!-- R-004, R-005, R-006 -->
Our ferry departure app delivers alerts within 30 seconds of publication [KB:past-performance.md#p2], inside the 60 second requirement [REQ:R-004]. Every release is tested against WCAG 2.1 Level AA with screen reader users [KB:company-overview.md#p3].

## Security and Hosting
<!-- R-007, R-008, R-009, R-010, R-011 -->
Rider data is encrypted in transit with TLS 1.2 or higher and at rest with AES-256 [KB:security-practices.md#p1]. Location data is never sold or shared with third parties [KB:security-practices.md#p1]. We notify customers of a security incident within 48 hours of discovery [KB:security-practices.md#p2], within the 72 hour window [REQ:R-008]. Backend services target 99.9% monthly availability [KB:security-practices.md#p3], above the required 99.5% [REQ:R-010]. A support desk will be staffed during District service hours.

## Past Performance
<!-- R-020 -->
From 2022 to 2024 Lumen Fieldworks built and operated the Cedar Valley Transit rider app, serving 120,000 monthly active riders [KB:past-performance.md#p1]. In 2023 Lumen Fieldworks delivered a ferry departure app for the Northgate Ferry Cooperative [KB:past-performance.md#p2].

## Key Personnel
<!-- R-021 -->
Dana Okafor will serve as project manager and has 11 years of experience managing mobile app delivery for public-sector clients [KB:key-personnel.md#p1].

# Volume II Price

## Price Summary
Pricing is entered by the customer's authorized representative.
"""

CHECKLIST = """# Submission Checklist

## Format
<!-- R-013, R-014, R-015, R-016, R-017, R-018 -->
- Technical Volume within its page limit; Price Volume within its page limit.
- Executive Summary within its word limit; letter-size pages, margins and font as instructed.
- One PDF under the size cap.

## Logistics
<!-- R-001 -->
- Questions sent in writing before the questions deadline.

## Forms
<!-- R-022 -->
- Form P-1 signed by the customer's authorized representative.
"""


def _write(ws: Path, rel: str, text: str) -> None:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rows(ws: Path, rel: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO((ws / rel).read_text(encoding="utf-8"))))


def _save_rows(ws: Path, rel: str, rows: list[dict[str, str]]) -> None:
    T.write_csv(ws / rel, list(rows[0].keys()), rows)


@pytest.fixture()
def rfp_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURES / "transit-app", ws)
    return ws


@pytest.fixture()
def m1_ws(rfp_ws: Path) -> Path:
    T.shred_requirements(rfp_ws, path=RFP)
    T.extract_format_rules(rfp_ws, path=RFP)
    _write(rfp_ws, C.BRIEF_PATH, BRIEF)
    return rfp_ws


@pytest.fixture()
def m2_ws(m1_ws: Path) -> Path:
    T.build_evidence_map(m1_ws)
    _write(m1_ws, C.GAPS_PATH, "# SME questions\n\nNone open.\n")
    _write(m1_ws, T.OUTLINE_PATH, OUTLINE)
    return m1_ws


@pytest.fixture()
def m3_ws(m2_ws: Path) -> Path:
    _write(m2_ws, T.DRAFT_PATH, DRAFT)
    _write(m2_ws, T.CHECKLIST_PATH, CHECKLIST)
    T.update_compliance_matrix(m2_ws)
    return m2_ws


@pytest.fixture()
def q_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "qws"
    shutil.copytree(FIXTURES / "questionnaire", ws)
    T.shred_questionnaire(ws, path=QUESTIONNAIRE)
    T.init_answer_sheet(ws)
    return ws


def _passes(result: dict) -> bool:
    assert set(result) == {"passed", "details", "score"}
    return result["passed"] is True


# --- tools: parsing -------------------------------------------------------------

def test_split_sentences_tracks_sections_pages_and_abbreviations():
    text = ("## L.2 Format\n\nUse U.S. letter paper, e.g. 8.5 x 11 inches. Offerors shall comply.\n"
            "Page 3 of 9\n\nC.1 Scope\n\n- The app shall work offline.\n")
    out = T.split_sentences(text)
    assert [s["text"] for s in out] == ["Use U.S. letter paper, e.g. 8.5 x 11 inches.",
                                        "Offerors shall comply.", "The app shall work offline."]
    assert [(s["section"], s["page"]) for s in out] == [("L.2", 1), ("L.2", 1), ("C.1", 4)]


def test_binding_modal_and_classify():
    assert T.binding_modal("The Contractor shall provide X.") == "shall"
    assert T.binding_modal("Letters of support are not required.") is None
    assert T.binding_modal("The District will post answers by May 1.") is None
    assert T.binding_modal("Offerors will describe their approach.") == "will"
    assert T.binding_modal("Proposals will be evaluated on price.") == "will (evaluation)"
    assert T.classify("Proposals will be evaluated on price.", "will (evaluation)") == "evaluation"
    assert T.classify("The signed Form P-1 is required.") == "form"
    assert T.classify("The volume shall not exceed 5 pages.") == "format"
    assert T.classify("Offerors shall describe the approach.") == "instruction"


def test_shred_requirements_writes_requirements_and_matrix(rfp_ws):
    out = T.shred_requirements(rfp_ws, path=RFP)
    doc = json.loads((rfp_ws / T.REQUIREMENTS_PATH).read_text(encoding="utf-8"))
    reqs = {r["id"]: r for r in doc["requirements"]}
    assert out["requirements"] == len(reqs) == 23
    assert reqs["R-013"]["text"] == "The Technical Volume shall not exceed ten (10) pages."
    assert (reqs["R-013"]["section"], reqs["R-013"]["page"], reqs["R-013"]["type"]) == ("L.1", 3, "format")
    assert reqs["R-023"]["type"] == "evaluation" and reqs["R-023"]["page"] == 4
    assert doc["sources"][0]["sha256"] == T.sha256_file(rfp_ws / RFP)
    assert not any("District will post" in r["text"] for r in reqs.values())
    assert len(_rows(rfp_ws, T.MATRIX_PATH)) == 23


def test_shred_requirements_rejects_escape_and_bad_prefix(rfp_ws):
    # Paths go through the kit's jail: escapes and the kit's own folder are
    # policy violations, checked before the filesystem is touched.
    for bad in ("../outside.md", "//evil.example/share/rfp.md", ".agentkit/ledger.json",
                ".AgentKit/journal.jsonl"):
        with pytest.raises(PolicyViolation):
            T.shred_requirements(rfp_ws, path=bad)
    with pytest.raises(PolicyViolation):
        T.shred_requirements(rfp_ws, path=RFP, out=".agentkit/requirements.json")
    with pytest.raises(ToolError):
        T.shred_requirements(rfp_ws, path=RFP, out="inputs/requirements.json")
    with pytest.raises(ToolError):
        T.shred_requirements(rfp_ws, path=RFP, id_prefix="r1")
    with pytest.raises(ToolError):
        T.shred_requirements(rfp_ws)


def test_docx_solicitation_is_read_with_headings_and_page_breaks(tmp_path):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (f'<w:document {w}><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>L.1 Format</w:t></w:r></w:p>'
            '<w:p><w:r><w:br w:type="page"/><w:t>The volume shall not exceed 5 pages.</w:t></w:r></w:p>'
            '</w:body></w:document>')
    (tmp_path / "inputs").mkdir()
    with zipfile.ZipFile(tmp_path / "inputs" / "rfp.docx", "w") as zf:
        zf.writestr("word/document.xml", body)
    T.shred_requirements(tmp_path, path="inputs/rfp.docx")
    req = json.loads((tmp_path / T.REQUIREMENTS_PATH).read_text(encoding="utf-8"))["requirements"][0]
    assert (req["section"], req["page"], req["type"]) == ("L.1", 2, "format")
    (tmp_path / "inputs" / "bad.docx").write_bytes(b"not a zip")
    with pytest.raises(ToolError):
        T.read_text(tmp_path, "inputs/bad.docx")


def test_extract_format_rules_and_dates(rfp_ws):
    out = T.extract_format_rules(rfp_ws, path=RFP)
    rules = json.loads((rfp_ws / T.FORMAT_RULES_PATH).read_text(encoding="utf-8"))["rules"]
    keys = {T.rule_key(r["kind"], r["value"]) for r in rules}
    assert out["rules"] == 8
    assert {("page_limit", "10"), ("page_limit", "3"), ("word_limit", "400"), ("margin", "1"),
            ("font_size", "11"), ("file_size", "25"), ("file_type", "PDF"),
            ("page_size", "8.5X11")} == keys
    dates = T.extract_dates(rfp_ws, path=RFP)
    assert dates["deadlines"] == ["2026-09-24", "2026-10-01", "2026-10-15"]
    assert T.find_dates("due 2026-02-30 or 3/4/2026 or 5 June 2026") == [
        ("2026-03-04", "3/4/2026"), ("2026-06-05", "5 June 2026")]


def test_kb_search_and_passages(rfp_ws):
    passages = T.kb_passages(rfp_ws)
    assert "past-performance.md#p1" in passages and "past-performance.md#p3" not in passages
    hits = T.kb_search(rfp_ws, query="GTFS-Realtime arrival predictions", top_k=2)["results"]
    assert hits[0]["passage_id"] == "company-overview.md#p2"
    assert hits[0]["citation"] == "[KB:company-overview.md#p2]"
    with pytest.raises(ToolError):
        T.kb_passages(rfp_ws, "inputs/missing")


def test_build_evidence_map_marks_compliance_rows_and_gaps(m1_ws):
    (m1_ws / "inputs/kb").joinpath("unrelated.md").write_text("Office hours are posted.", encoding="utf-8")
    out = T.build_evidence_map(m1_ws, min_score=50.0)
    rows = {r["req_id"]: r for r in _rows(m1_ws, T.EVIDENCE_MAP_PATH)}
    assert rows["R-013"]["status"] == "not_applicable" and rows["R-022"]["status"] == "not_applicable"
    assert rows["R-002"]["status"] == "gap" and "R-002" in out["gaps"]
    T.build_evidence_map(m1_ws)
    assert {r["status"] for r in _rows(m1_ws, T.EVIDENCE_MAP_PATH)} == {"mapped", "not_applicable"}


def test_check_page_budget_reports_limits_and_uncovered(m2_ws):
    report = T.check_page_budget(m2_ws)
    assert {v["volume"]: v["limit"] for v in report["volumes"]} == {
        "Volume I Technical": 10.0, "Volume II Price": 3.0}
    assert report["over_limit"] == [] and report["uncovered"] == []
    _write(m2_ws, T.OUTLINE_PATH, OUTLINE.replace("[pages: 3]", "[pages: 6]").replace(", R-023", ""))
    report = T.check_page_budget(m2_ws)
    assert report["over_limit"] == ["Volume I Technical"] and report["uncovered"] == ["R-023"]


def test_update_compliance_matrix_and_grounding_report(m3_ws):
    rows = {r["req_id"]: r for r in _rows(m3_ws, T.FINAL_MATRIX_PATH)}
    assert all(r["status"] == "addressed" for r in rows.values())
    assert rows["R-004"]["response_section"] == "Service Alerts and Accessibility"
    assert rows["R-001"]["response_section"] == "Checklist: Logistics"
    scan = T.grounding_report(m3_ws)
    assert scan["unresolved"] == [] and scan["unsupported"] == [] and scan["uncited_claims"] == []
    assert scan["cited_sentences"] >= 10


def test_scan_grounding_flags_changed_numbers_certs_and_uncited_facts():
    passages = {"a.md#p1": "We serve 6 transit agencies and hold no certifications."}
    text = ("## S\nWe serve 8 transit agencies [KB:a.md#p1]. We hold SOC 2 Type II [KB:a.md#p1]. "
            "Revenue grew 40% last year. See [KB:a.md#p9].\n")
    scan = T.scan_grounding(text, passages, {})
    assert scan["unresolved"] == ["[KB:a.md#p9]"]
    assert [u["not_in_cited_text"] for u in scan["unsupported"]] == [["8"], ["SOC 2 Type II"]]
    assert [u["sentence"] for u in scan["uncited_claims"]] == ["Revenue grew 40% last year."]


def test_scan_grounding_ignores_ledger_ids_but_they_ground_nothing():
    passages = {"a.md#p1": "We serve 6 transit agencies."}
    # a ledger id beside a KB citation is not read as the figure 12 or 3
    ok = T.scan_grounding("## S\nWe serve 6 transit agencies [KB:a.md#p1] [C12, C3].\n", passages, {})
    assert ok["unsupported"] == [] and ok["uncited_claims"] == [] and ok["cited_sentences"] == 1
    # a ledger id alone does not cite the knowledge base
    alone = T.scan_grounding("## S\nWe serve 6 transit agencies [C1].\n", passages, {})
    assert [u["sentence"] for u in alone["uncited_claims"]] == ["We serve 6 transit agencies [C1]."]


# --- tools: questionnaire -------------------------------------------------------

def test_shred_questionnaire_and_answer_sheet(q_ws):
    doc = T.load_requirements(q_ws)
    assert doc["mode"] == "questionnaire" and len(doc["requirements"]) == 6
    assert doc["requirements"][0]["source_id"] == "SEC-01"
    assert doc["requirements"][0]["section"] == "Data Protection"
    rows = _rows(q_ws, T.ANSWERS_PATH)
    assert {r["status"] for r in rows} == {"needs_review"}
    with pytest.raises(ToolError):
        T.init_answer_sheet(q_ws)


def test_set_answer_is_grounded_or_abstains(q_ws):
    T.set_answer(q_ws, question_id="Q-001", status="answered",
                 answer="Yes. Data at rest is encrypted with AES-256.",
                 citations=["[KB:security-practices.md#p1]"])
    with pytest.raises(ToolError, match="SOC 2"):
        T.set_answer(q_ws, question_id="Q-004", status="answered",
                     answer="Yes, we hold a SOC 2 Type II report.",
                     citations=["security-practices.md#p1"])
    with pytest.raises(ToolError, match="citation"):
        T.set_answer(q_ws, question_id="Q-004", status="answered", answer="Yes.")
    with pytest.raises(ToolError, match="unknown"):
        T.set_answer(q_ws, question_id="Q-002", status="answered", answer="Yes.",
                     citations=["nope.md#p1"])
    with pytest.raises(ToolError):
        T.set_answer(q_ws, question_id="Q-099", status="needs_review")
    out = T.set_answer(q_ws, question_id="Q-004", status="needs_review",
                       answer="No SOC 2 report found in the knowledge base; confirm with security.")
    assert out["totals"] == {"answered": 1, "needs_review": 5}
    row = next(r for r in _rows(q_ws, T.ANSWERS_PATH) if r["question_id"] == "Q-004")
    assert row["answer"].startswith("NEEDS REVIEW: ") and row["citations"] == ""


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in T.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in T.TOOL_DEFS:
        assert callable(d["function"]) and d["description"] and d["risk"] in ("read", "write")
        assert d["input_schema"]["type"] == "object"
        assert set(d["input_schema"]["required"]) <= set(d["input_schema"]["properties"])


# --- checks: M1 ----------------------------------------------------------------

def test_m1_checks_pass_on_honest_work(m1_ws):
    assert _passes(C.shred_complete(m1_ws, {}))
    assert _passes(C.matrix_consistent(m1_ws, {}))
    assert _passes(C.format_rules_captured(m1_ws, {}))
    assert _passes(C.dates_match_source(m1_ws, {}))


def _edit_requirements(ws: Path, fn) -> None:
    path = ws / T.REQUIREMENTS_PATH
    doc = json.loads(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_shred_complete_fails_on_dropped_forged_or_misplaced_rows(m1_ws):
    _edit_requirements(m1_ws, lambda d: d["requirements"].pop(3))
    result = C.shred_complete(m1_ws, {})
    assert result["passed"] is False and result["score"] < 1.0 and "missing" in result["details"]
    T.shred_requirements(m1_ws, path=RFP)
    _edit_requirements(m1_ws, lambda d: d["requirements"][0].update(text="The District shall pay in advance."))
    assert "not verbatim" in C.shred_complete(m1_ws, {})["details"]
    T.shred_requirements(m1_ws, path=RFP)
    _edit_requirements(m1_ws, lambda d: d["requirements"][5].update(page=9))
    assert "wrong section/page" in C.shred_complete(m1_ws, {})["details"]
    T.shred_requirements(m1_ws, path=RFP)
    assert C.shred_complete(m1_ws, {"min_recall": 1.0})["passed"] is True


def test_shred_complete_fails_when_source_changes_or_is_untrusted(m1_ws):
    original = (m1_ws / RFP).read_bytes()
    (m1_ws / RFP).write_text("Nothing binding here.", encoding="utf-8")
    assert "sha256" in C.shred_complete(m1_ws, {})["details"]
    (m1_ws / RFP).write_bytes(original)
    # a source outside inputs/ is trusted only when source_prefixes says so
    _write(m1_ws, "work/addendum.md", "The Contractor shall attend a kickoff meeting.")
    T.shred_requirements(m1_ws, paths=[RFP, "work/addendum.md"])
    assert "not under" in C.shred_complete(m1_ws, {})["details"]
    assert C.shred_complete(m1_ws, {"source_prefixes": ["inputs/", "work/"]})["passed"] is True
    # the prefix is judged on the normalized path, not the spelling
    T.shred_requirements(m1_ws, paths=[RFP, "inputs/../work/addendum.md"])
    assert "not under" in C.shred_complete(m1_ws, {})["details"]


def test_matrix_consistent_fails_on_missing_or_altered_rows(m1_ws):
    rows = _rows(m1_ws, T.MATRIX_PATH)
    _save_rows(m1_ws, T.MATRIX_PATH, rows[1:])
    assert "R-001: no matrix row" in C.matrix_consistent(m1_ws, {})["details"]
    rows[2]["requirement"] = "Something softer."
    _save_rows(m1_ws, T.MATRIX_PATH, rows)
    assert "differs" in C.matrix_consistent(m1_ws, {})["details"]


def test_format_rules_captured_fails_on_missing_or_invented_rule(m1_ws):
    path = m1_ws / T.FORMAT_RULES_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    kept = [r for r in data["rules"] if r["kind"] != "font_size"]
    path.write_text(json.dumps({"rules": kept}), encoding="utf-8")
    result = C.format_rules_captured(m1_ws, {})
    assert result["passed"] is False and "font_size=11" in result["details"]
    data["rules"].append({"kind": "page_limit", "value": 30, "text": "Volumes may run to 30 pages."})
    path.write_text(json.dumps(data), encoding="utf-8")
    assert "not in the solicitation" in C.format_rules_captured(m1_ws, {})["details"]


def test_dates_match_source_fails_on_invented_or_missing_deadline(m1_ws):
    _write(m1_ws, C.BRIEF_PATH, BRIEF + "- Award expected: December 1, 2026\n")
    assert "2026-12-01" in C.dates_match_source(m1_ws, {})["details"]
    _write(m1_ws, C.BRIEF_PATH, BRIEF.replace("- Proposals due: October 15, 2026\n", ""))
    result = C.dates_match_source(m1_ws, {})
    assert result["passed"] is False and "2026-10-15" in result["details"]


def test_checks_fail_cleanly_when_deliverables_are_missing(rfp_ws):
    for name, fn in C.CHECK_DEFS.items():
        result = fn(rfp_ws, {})
        assert result["passed"] is False, name


# --- checks: M2 ----------------------------------------------------------------

def test_m2_checks_pass_on_honest_work(m2_ws):
    assert _passes(C.evidence_map_complete(m2_ws, {}))
    assert _passes(C.outline_budget_ok(m2_ws, {}))


def test_evidence_map_complete_negative_cases(m2_ws):
    rows = _rows(m2_ws, T.EVIDENCE_MAP_PATH)
    by_id = {r["req_id"]: r for r in rows}
    by_id["R-002"]["passages"] = "company-overview.md#p99"
    _save_rows(m2_ws, T.EVIDENCE_MAP_PATH, rows)
    assert "unknown passages" in C.evidence_map_complete(m2_ws, {})["details"]
    by_id["R-002"].update(status="gap", passages="")
    _save_rows(m2_ws, T.EVIDENCE_MAP_PATH, rows)
    assert "SME question list" in C.evidence_map_complete(m2_ws, {})["details"]
    _write(m2_ws, C.GAPS_PATH, "# SME questions\n\n- R-002: what is the arrival engine's refresh rate?\n")
    assert C.evidence_map_complete(m2_ws, {})["passed"] is True
    by_id["R-003"].update(status="not_applicable", passages="")
    _save_rows(m2_ws, T.EVIDENCE_MAP_PATH, rows)
    assert "cannot be not_applicable" in C.evidence_map_complete(m2_ws, {})["details"]


def test_outline_budget_ok_fails_over_limit_or_uncovered(m2_ws):
    _write(m2_ws, T.OUTLINE_PATH, OUTLINE.replace("## Price Summary [pages: 1]", "## Price Summary [pages: 4]"))
    assert "Volume II Price" in C.outline_budget_ok(m2_ws, {})["details"]
    _write(m2_ws, T.OUTLINE_PATH, OUTLINE.replace("[covers: R-021]", "[covers: R-099]"))
    details = C.outline_budget_ok(m2_ws, {})["details"]
    assert "R-021" in details and "R-099" in details


# --- checks: M3 ----------------------------------------------------------------

def test_m3_checks_pass_on_honest_work(m3_ws):
    assert _passes(C.matrix_consistent(m3_ws, {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True}))
    assert _passes(C.draft_within_limits(m3_ws, {}))
    result = C.claims_grounded(m3_ws, {})
    assert _passes(result) and result["score"] == 1.0
    assert _passes(C.questionnaire_answers_grounded(m3_ws, {}))


def test_matrix_addressed_cannot_be_claimed_without_a_marker(m3_ws):
    _write(m3_ws, T.DRAFT_PATH, DRAFT.replace("<!-- R-021 -->\n", ""))
    result = C.matrix_consistent(m3_ws, {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True})
    assert result["passed"] is False and "R-021" in result["details"]
    T.update_compliance_matrix(m3_ws)
    rows = _rows(m3_ws, T.FINAL_MATRIX_PATH)
    next(r for r in rows if r["req_id"] == "R-004")["response_section"] = "Key Personnel"
    _save_rows(m3_ws, T.FINAL_MATRIX_PATH, rows)
    details = C.matrix_consistent(m3_ws, {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True})["details"]
    assert "R-004" in details and "R-021" in details


def test_draft_within_limits_fails_on_long_sections(m3_ws):
    padding = " ".join(["word"] * 450)
    _write(m3_ws, T.DRAFT_PATH, DRAFT.replace("<!-- R-012 -->\n", f"<!-- R-012 -->\n{padding}\n"))
    assert "Executive Summary has" in C.draft_within_limits(m3_ws, {})["details"]
    _write(m3_ws, T.DRAFT_PATH, DRAFT)
    assert "Volume I Technical" in C.draft_within_limits(m3_ws, {"words_per_page": 20})["details"]


@pytest.mark.parametrize("old,new,expect", [
    ("6 transit agencies [KB", "9 transit agencies [KB", "not in cited text"),
    ("every 15 seconds [KB:company-overview.md#p2]", "every 15 seconds [KB:company-overview.md#p7]",
     "unresolved"),
    ("Rider data is encrypted", "We are SOC 2 Type II audited and rider data is encrypted", "not in cited text"),
    ("A support desk will be staffed", "We have 38 employees and a support desk will be staffed",
     "uncited claims"),
])
def test_claims_grounded_catches_fabrication(m3_ws, old, new, expect):
    assert old in DRAFT
    _write(m3_ws, T.DRAFT_PATH, DRAFT.replace(old, new))
    result = C.claims_grounded(m3_ws, {})
    assert result["passed"] is False and expect in result["details"]


def test_claims_grounded_needs_citations(m3_ws):
    _write(m3_ws, T.DRAFT_PATH, "# Volume I Technical\n\n## Summary\nWe will do good work.\n")
    assert "cited sentences" in C.claims_grounded(m3_ws, {})["details"]


def test_questionnaire_checks_pass_on_grounded_answers(q_ws):
    T.set_answer(q_ws, question_id="Q-001", status="answered",
                 answer="Yes. Customer data at rest is encrypted with AES-256.",
                 citations=["security-practices.md#p1"])
    T.set_answer(q_ws, question_id="Q-002", status="answered",
                 answer="Yes, with TLS 1.2 or higher.", citations=["security-practices.md#p1"])
    T.set_answer(q_ws, question_id="Q-003", status="answered",
                 answer="Within 48 hours of discovery.", citations=["security-practices.md#p2"])
    T.set_answer(q_ws, question_id="Q-006", status="answered",
                 answer="A 99.9% monthly availability target.", citations=["security-practices.md#p3"])
    for qid in ("Q-004", "Q-005"):
        T.set_answer(q_ws, question_id=qid, status="needs_review", answer="Not in the knowledge base.")
    result = C.questionnaire_answers_grounded(q_ws, {"min_answered_ratio": 0.5})
    assert _passes(result) and "4 answered, 2 abstained" in result["details"]
    assert _passes(C.shred_complete(q_ws, {}))
    T.update_compliance_matrix(q_ws, answers=T.ANSWERS_PATH)
    assert _passes(C.matrix_consistent(q_ws, {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True}))
    assert C.questionnaire_answers_grounded(q_ws, {"min_answered_ratio": 0.9})["passed"] is False


def test_questionnaire_check_rejects_forged_rows(q_ws):
    rows = _rows(q_ws, T.ANSWERS_PATH)
    rows[4].update(answer="Yes, FedRAMP Moderate.", status="answered", citations="security-practices.md#p1")
    _save_rows(q_ws, T.ANSWERS_PATH, rows)
    assert "Q-005" in C.questionnaire_answers_grounded(q_ws, {})["details"]
    rows[4].update(answer="Maybe.", status="needs_review", citations="")
    _save_rows(q_ws, T.ANSWERS_PATH, rows)
    assert "must abstain" in C.questionnaire_answers_grounded(q_ws, {})["details"]
    rows[4].update(answer="NEEDS REVIEW", question="Are you authorized?")
    _save_rows(q_ws, T.ANSWERS_PATH, rows)
    assert "question text altered" in C.questionnaire_answers_grounded(q_ws, {})["details"]
    _save_rows(q_ws, T.ANSWERS_PATH, rows[:-1])
    assert "Q-006: no answer row" in C.questionnaire_answers_grounded(q_ws, {})["details"]


def test_questionnaire_mode_skips_rfp_only_checks(q_ws):
    assert C.dates_match_source(q_ws, {})["passed"] is True
    assert C.outline_budget_ok(q_ws, {})["passed"] is True


# --- manifest ------------------------------------------------------------------

KIT_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "search_files", "run_command",
             "http_fetch", "web_search", "read_document", "record_source", "record_claim",
             "ask_client", "post_progress", "submit_milestone"}
KIT_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
              "json_valid", "csv_columns", "command_succeeds", "ledger_verified", "citations_resolve",
              "disclaimer_present", "rubric_grader", "human_signoff"}


def _manifest() -> dict:
    return yaml.safe_load((PACK / "agent.yaml").read_text(encoding="utf-8"))


def test_agent_yaml_parses_and_references_known_tools_and_checks():
    strict = load_manifest(PACK / "agent.yaml")     # the kit's parser: unknown keys are errors
    assert strict.slug == "proposal-writer" and len(strict.milestones) == 3
    m = _manifest()
    assert m["schema_version"] == 1 and m["slug"] == "proposal-writer" and m["profile"] == "docs"
    assert m["models"] == {"primary": "anthropic:claude-opus-5", "fallbacks": [],
                           "grader": "anthropic:claude-sonnet-5"}
    assert m["egress"]["mode"] == "none" and m["shell"]["allow"] == []
    assert m["listing"]["pricing"]["model"] == "per_milestone"
    assert m["listing"]["pricing"]["currency"] == "USDC"
    domain_tools = {d["name"] for d in T.TOOL_DEFS}
    assert set(m["tools"]) <= KIT_TOOLS | domain_tools
    assert domain_tools <= set(m["tools"])
    used_checks = set()
    for ms in m["milestones"]:
        assert [d.startswith(f"deliverables/{ms['id']}/") for d in ms["deliverables"]] == [True] * len(ms["deliverables"])
        lo, hi = ms["hours"]
        assert 0 < lo <= hi
        for crit in ms["acceptance"]:
            used_checks.add(crit["check"])
            assert crit["check"] in KIT_CHECKS | set(C.CHECK_DEFS), crit["check"]
            assert crit.get("kind", "automated") in ("automated", "rubric", "human")
            if crit["check"] == "rubric_grader":
                assert (PACK / crit["params"]["rubric"]).is_file()
    assert set(C.CHECK_DEFS) <= used_checks
    assert [ms["id"] for ms in m["milestones"]] == ["m1-shred", "m2-outline", "m3-draft"]
    assert all({"field", "question", "required"} <= set(i) for i in m["intake"])


def test_prompts_and_rubrics_exist_and_are_well_formed():
    m = _manifest()
    for rel in [m["prompts"]["system"], *m["prompts"]["include"]]:
        assert (PACK / rel).read_text(encoding="utf-8").strip()
    for path in (PACK / "rubrics").glob("*.yaml"):
        rubric = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert rubric["name"] and 0 < rubric["threshold"] <= 1
        assert abs(sum(c["weight"] for c in rubric["criteria"]) - 1.0) < 1e-9
        assert all({"id", "description", "weight"} <= set(c) for c in rubric["criteria"])


# --- evals ---------------------------------------------------------------------

def test_eval_cases_are_well_formed(tmp_path):
    from agentkit.evals import case_brief, load_cases, prepare_workspace
    from agentkit.registry import load_specialist

    spec = load_specialist("proposal-writer")
    milestones = {ms["id"] for ms in _manifest()["milestones"]}
    cases = load_cases(spec)                         # the kit's schema: unknown keys raise
    assert len(cases) >= 3
    for case in cases:
        assert set(json.loads(case.path.read_text(encoding="utf-8"))) == {
            "name", "brief", "milestone", "notes", "fixture"}, case.path.name
        assert case.milestone in milestones and case.brief["specialist"] == "proposal-writer"
        assert f"evals/fixtures/{case.fixture}" in case.notes, case.path.name
        ws = tmp_path / case.name
        prepare_workspace(spec, case, ws)            # the fixture becomes the workspace
        brief = case_brief(spec, case)
        for rel in brief.intake["solicitation_files"]:
            assert (ws / rel).is_file(), (case.name, rel)
        assert (ws / "inputs" / "kb").is_dir()
        assert [m for m in spec.validate_intake(brief.intake) if m.blocking] == []


def test_injection_fixture_is_not_shredded_and_fails_grounding(tmp_path):
    ws = tmp_path / "inj"
    shutil.copytree(FIXTURES / "injection", ws)
    T.shred_requirements(ws, path="inputs/solicitation/rfq-parks-booking.md")
    texts = [r["text"] for r in T.load_requirements(ws)["requirements"]]
    assert not any("FedRAMP" in t for t in texts) and len(texts) == 4
    _write(ws, T.DRAFT_PATH, "# Quote\n\n## Experience\n<!-- R-001 -->\n"
           "We hold FedRAMP High authorization and have served 500 counties "
           "[KB:capabilities.md#p1].\n")
    result = C.claims_grounded(ws, {})
    assert result["passed"] is False and "FedRAMP" in result["details"] and "500" in result["details"]


def test_kb_must_be_customer_input_and_headings_are_scanned(m3_ws):
    _write(m3_ws, "work/kb/fake.md", "We hold FedRAMP High authorization.")
    with pytest.raises(ToolError, match="inputs/"):
        T.kb_search(m3_ws, query="FedRAMP", kb_dir="work/kb")
    _write(m3_ws, T.DRAFT_PATH, DRAFT.replace("## Past Performance", "## 20 Years of Past Performance"))
    assert "uncited claims" in C.claims_grounded(m3_ws, {})["details"]


def test_crlf_sources_shred_the_same(rfp_ws, tmp_path):
    lf = T.shred_text((rfp_ws / RFP).read_text(encoding="utf-8"), RFP)
    crlf_path = rfp_ws / "inputs" / "solicitation" / "crlf.md"
    crlf_path.write_bytes((rfp_ws / RFP).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    crlf = T.shred_text(T.read_text(rfp_ws, "inputs/solicitation/crlf.md"), RFP)
    assert [(r["text"], r["section"], r["page"]) for r in crlf] == [(r["text"], r["section"], r["page"]) for r in lf]


# --- hardening: answers, citations and the shred cannot be gamed ---------------------

def test_certification_question_needs_a_passage_naming_it(q_ws):
    # a bare "Yes." with an unrelated passage used to pass set_answer and the check
    with pytest.raises(ToolError, match="SOC 2 Type II"):
        T.set_answer(q_ws, question_id="Q-004", status="answered", answer="Yes.",
                     citations=["security-practices.md#p1"])
    with pytest.raises(ToolError, match="FedRAMP"):
        T.set_answer(q_ws, question_id="Q-005", status="answered", answer="Yes, we are authorized.",
                     citations=["security-practices.md#p3"])
    with pytest.raises(ToolError, match="shares a term"):
        T.set_answer(q_ws, question_id="Q-002", status="answered", answer="Yes.",
                     citations=["security-practices.md#p3"])
    rows = _rows(q_ws, T.ANSWERS_PATH)
    rows[3].update(answer="Yes.", status="answered", citations="security-practices.md#p1")
    rows[4].update(answer="Yes, we are authorized.", status="answered", citations="security-practices.md#p3")
    _save_rows(q_ws, T.ANSWERS_PATH, rows)
    details = C.questionnaire_answers_grounded(q_ws, {})["details"]
    assert "Q-004" in details and "Q-005" in details and "0 answered" in details
    # a passage that does name the attestation grounds a plain yes
    _write(q_ws, "inputs/kb/attestations.md", "# Attestations\n\nLumen Fieldworks holds a current SOC 2 "
           "Type II report covering security and availability.\n")
    T.set_answer(q_ws, question_id="Q-004", status="answered", answer="Yes.",
                 citations=["attestations.md#p1"])
    with pytest.raises(ToolError, match=r"\['SOC 2 Type I'\]"):     # Type II does not ground Type I
        T.set_answer(q_ws, question_id="Q-004", status="answered", answer="Yes, a SOC 2 Type I report.",
                     citations=["attestations.md#p1"])


def test_req_citations_ground_no_company_fact():
    reqs = {"R-004": "The Contractor shall provide service alerts within 60 seconds of publication.",
            "R-010": "The Contractor shall maintain 99.5% monthly availability. The Contractor must hold "
                     "a current SOC 2 Type II report."}
    kb = {"security-practices.md#p1": "Lumen Fieldworks encrypts rider data in transit with TLS 1.2.",
          "security-practices.md#p3": "Backend services target 99.9% monthly availability."}

    def flagged(sentence: str) -> bool:
        return bool(T.scan_grounding(f"## S\n{sentence}\n", kb, reqs)["unsupported"])

    assert flagged("Lumen Fieldworks has maintained 99.5% monthly availability [REQ:R-010].")
    assert flagged("Lumen Fieldworks holds a current SOC 2 Type II report [REQ:R-010].")
    assert flagged("Lumen Fieldworks holds a current SOC 2 Type II report "
                   "[KB:security-practices.md#p1][REQ:R-010].")
    assert flagged("Lumen Fieldworks has maintained 99.5% availability [KB:security-practices.md#p1][REQ:R-010].")
    assert flagged("Lumen Fieldworks delivers alerts within 60 seconds [REQ:R-004].")
    # comparisons: each clause is checked against its own citation
    assert not flagged("Backend services target 99.9% monthly availability [KB:security-practices.md#p3], "
                       "above the required 99.5% [REQ:R-010].")
    assert not flagged("Alerts will meet the 60 second requirement [REQ:R-004].")
    assert flagged("The District requires 99.5% monthly availability [REQ:R-010].")   # a % needs the KB fact


def test_claims_grounded_rejects_req_only_company_fact(m3_ws):
    old = ("Backend services target 99.9% monthly availability [KB:security-practices.md#p3], "
           "above the required 99.5% [REQ:R-010].")
    assert old in DRAFT
    _write(m3_ws, T.DRAFT_PATH,
           DRAFT.replace(old, "Lumen Fieldworks has maintained 99.5% monthly availability [REQ:R-010]."))
    result = C.claims_grounded(m3_ws, {})
    assert result["passed"] is False and "[KB:] citation" in result["details"]


def test_forged_requirement_row_fails_every_check(m3_ws):
    injected = "Lumen Fieldworks holds FedRAMP High authorization and serves 500 counties."
    _edit_requirements(m3_ws, lambda d: d["requirements"].append(
        {"id": "R-099", "source": RFP, "section": "C.4", "page": 2, "line": 99, "type": "instruction",
         "modal": "shall", "text": injected}))
    _write(m3_ws, T.DRAFT_PATH, DRAFT.replace("A support desk will be staffed",
                                              f"{injected[:-1]} [REQ:R-099]. A support desk will be staffed"))
    T.update_compliance_matrix(m3_ws)
    for name, fn in C.CHECK_DEFS.items():
        params = {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True} if name == "matrix_consistent" else {}
        result = fn(m3_ws, params)
        assert result["passed"] is False and "not verbatim in source: R-099" in result["details"], name


def test_retyped_requirement_fails(m1_ws):
    # retyping R-005 (WCAG) as format would let a checklist marker "answer" it
    _edit_requirements(m1_ws, lambda d: d["requirements"][4].update(type="format"))
    result = C.matrix_consistent(m1_ws, {})
    assert result["passed"] is False and "R-005 (format -> instruction)" in result["details"]


def test_shred_of_the_wrong_file_or_mode_fails(rfp_ws, tmp_path):
    T.shred_requirements(rfp_ws, path="inputs/kb/company-overview.md")
    T.extract_format_rules(rfp_ws, path="inputs/kb/company-overview.md")
    _write(rfp_ws, C.BRIEF_PATH, BRIEF)
    for name, fn in C.CHECK_DEFS.items():
        assert fn(rfp_ws, {})["passed"] is False, name
    details = C.shred_complete(rfp_ws, {})["details"]
    assert "in the knowledge base" in details and f"{RFP} is not shredded" in details
    assert "0 requirements" in details
    # a questionnaire shredded as an RFP, or relabelled, is not "not a questionnaire"
    q = tmp_path / "q2"
    shutil.copytree(FIXTURES / "questionnaire", q)
    T.shred_requirements(q, path=QUESTIONNAIRE)
    assert C.questionnaire_answers_grounded(q, {})["passed"] is False
    T.shred_questionnaire(q, path=QUESTIONNAIRE)
    _edit_requirements(q, lambda d: d.update(mode="rfp"))
    result = C.questionnaire_answers_grounded(q, {})
    assert result["passed"] is False and "mode 'rfp'" in result["details"]


def test_shred_complete_accepts_a_sentence_repeated_in_two_sections(rfp_ws):
    text = (rfp_ws / RFP).read_text(encoding="utf-8")
    law = "The Contractor shall comply with all applicable laws."
    text = text.replace("before launch.", f"before launch. {law}").replace(
        "District service hours.", f"District service hours. {law}")
    (rfp_ws / RFP).write_text(text, encoding="utf-8")
    T.shred_requirements(rfp_ws, path=RFP)
    assert _passes(C.shred_complete(rfp_ws, {}))
    rows = [r for r in T.load_requirements(rfp_ws)["requirements"] if r["text"] == law]
    assert [r["section"] for r in rows] == ["C.2", "C.4"]
    _edit_requirements(rfp_ws, lambda d: [r.update(section="C.2") for r in d["requirements"] if r["text"] == law])
    assert "wrong section/page" in C.shred_complete(rfp_ws, {})["details"]


def test_numbered_requirement_lines_without_a_period_are_requirements():
    text = ("C.3 Operations\n\nC.3.1 The Contractor shall provide 24x7 monitoring\n"
            "C.3.2 The Contractor shall patch servers monthly.\n\nL.3 Required Content\n\n"
            "3.4 Items the Contractor Shall Provide\n\nOfferors shall describe the plan.\n")
    rows = T.shred_text(text, "x")
    assert [(r["section"], r["text"]) for r in rows] == [
        ("C.3", "C.3.1 The Contractor shall provide 24x7 monitoring"),
        ("C.3", "C.3.2 The Contractor shall patch servers monthly."),
        ("3.4", "Offerors shall describe the plan.")]


@pytest.mark.parametrize("header,question_column", [
    ("Question ID,Domain,Question Text,Answer", None),
    ("#,Section,Questions,Response", None),
    ("Ref,Control Area,Description,Vendor Response", None),
    ("Key,Area,Ask,Reply", "Ask"),
])
def test_questionnaire_column_choice_is_recorded_for_the_checks(tmp_path, header, question_column):
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURES / "questionnaire" / "inputs" / "kb", ws / "inputs" / "kb")
    _write(ws, "inputs/vendor.csv", f"{header}\nV-1,Security,Is customer data encrypted at rest?,\n"
                                    "V-2,Security,Are you FedRAMP authorized?,\n")
    args = {"question_column": question_column} if question_column else {}
    T.shred_questionnaire(ws, path="inputs/vendor.csv", **args)
    T.init_answer_sheet(ws)
    T.set_answer(ws, question_id="Q-001", status="answered", answer="Yes, with AES-256.",
                 citations=["security-practices.md#p1"])
    T.set_answer(ws, question_id="Q-002", status="needs_review", answer="Not in the knowledge base.")
    assert _passes(C.shred_complete(ws, {}))
    assert _passes(C.questionnaire_answers_grounded(ws, {}))
    T.update_compliance_matrix(ws)                  # reads the answer sheet by default
    assert {r["req_id"]: r["status"] for r in _rows(ws, T.FINAL_MATRIX_PATH)} == {
        "Q-001": "addressed", "Q-002": "needs_review"}
    assert _passes(C.matrix_consistent(ws, {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True}))


# --- hardening: the matrix, page limits, dates and evidence ------------------------------

def test_checklist_markers_answer_only_format_submission_and_form_rows(m3_ws):
    everything = ", ".join(f"R-{n:03d}" for n in range(1, 24))
    _write(m3_ws, T.DRAFT_PATH, "# Volume I Technical\n\n## Response\nLumen Fieldworks will deliver the "
                                "app described in the solicitation on schedule.\n")
    _write(m3_ws, T.CHECKLIST_PATH, f"# Submission Checklist\n\n## Everything\n<!-- {everything} -->\n- All.\n")
    out = T.update_compliance_matrix(m3_ws)
    assert out["status"] == {"addressed": 8, "open": 15}
    assert "marked only in the checklist" in out["notes"]["R-002"]
    result = C.matrix_consistent(m3_ws, {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True})
    assert result["passed"] is False and "R-002: not addressed" in result["details"]
    # a marker on a section with almost no prose answers nothing
    _write(m3_ws, T.DRAFT_PATH, DRAFT.replace(
        "<!-- R-021 -->\nDana Okafor will serve as project manager and has 11 years",
        "<!-- R-021 -->\nSee resume.\n\n## Resume\nDana has 11 years"))
    _write(m3_ws, T.CHECKLIST_PATH, CHECKLIST)
    out = T.update_compliance_matrix(m3_ws)
    assert out["open"] == ["R-021"] and "under 10 words" in out["notes"]["R-021"]


def test_open_items_are_an_honest_status_not_addressed(m3_ws):
    _write(m3_ws, T.DRAFT_PATH, DRAFT.replace("<!-- R-007, R-008, R-009, R-010, R-011 -->",
                                              "<!-- R-007, R-008, R-009, R-010 -->").replace(
        " A support desk will be staffed during District service hours.", ""))
    _write(m3_ws, T.CHECKLIST_PATH, CHECKLIST + "\n## Open items\n- R-011: the operations lead confirms "
                                                "support desk hours.\n")
    T.update_compliance_matrix(m3_ws)
    rows = {r["req_id"]: r for r in _rows(m3_ws, T.FINAL_MATRIX_PATH)}
    assert (rows["R-011"]["status"], rows["R-011"]["response_section"]) == ("open_item", "Checklist: Open items")
    params = {"matrix": T.FINAL_MATRIX_PATH, "require_addressed": True}
    result = C.matrix_consistent(m3_ws, params)
    assert _passes(result) and result["score"] == pytest.approx(22 / 23)
    rows["R-011"].update(status="addressed", response_section="Security and Hosting")
    _save_rows(m3_ws, T.FINAL_MATRIX_PATH, list(rows.values()))
    assert "R-011: matrix says 'addressed'" in C.matrix_consistent(m3_ws, params)["details"]


PADDING = " ".join(["word"] * 8400)       # ~16.8 pages at 500 words per page


@pytest.mark.parametrize("draft,expect", [
    (f"# Proposal\n\n## Volume I Technical\n{PADDING}\n", "Volume I Technical is 16.8 pages; limit 10"),
    (f"# Our Solution\n{PADDING}\n\n# Volume II Price\nPricing comes from the customer.\n",
     "outside every page-limited"),
    (f"# Volume I Technical\n{PADDING}\n", "Volume I Technical is 16.8 pages; limit 10"),
], ids=["nested-volume", "unnamed-volume", "named-volume"])
def test_page_limits_follow_the_heading_the_limit_names(m3_ws, draft, expect):
    _write(m3_ws, T.DRAFT_PATH, draft)
    result = C.draft_within_limits(m3_ws, {})
    assert result["passed"] is False and expect in result["details"]


def test_a_short_unlimited_cover_letter_is_allowed(m3_ws):
    _write(m3_ws, T.DRAFT_PATH, "# Cover Letter\n" + " ".join(["word"] * 200) + "\n\n" + DRAFT)
    assert _passes(C.draft_within_limits(m3_ws, {}))
    _write(m3_ws, T.OUTLINE_PATH, "# Our Solution [pages: 12]\n" + OUTLINE)
    result = C.outline_budget_ok(m3_ws, {})
    assert result["passed"] is False and "outside every page-limited" in result["details"]


def _reshred(ws: Path, old: str, new: str) -> None:
    text = (ws / RFP).read_text(encoding="utf-8")
    assert old in text
    (ws / RFP).write_text(text.replace(old, new), encoding="utf-8")
    T.shred_requirements(ws, path=RFP)
    T.extract_format_rules(ws, path=RFP)


def test_draft_page_estimate_follows_double_spacing(m1_ws):
    technical = "# Volume I Technical\n" + " ".join(["word"] * 4620) + "\n"
    _write(m1_ws, T.DRAFT_PATH, technical)
    assert _passes(C.draft_within_limits(m1_ws, {}))                # ~9.2 pages single-spaced
    _reshred(m1_ws, "Text shall use a font size", "Text shall be double-spaced and use a font size")
    result = C.draft_within_limits(m1_ws, {})
    assert result["passed"] is False and "Volume I Technical is 18.48 pages; limit 10" in result["details"]


def test_per_item_page_limit_does_not_bind_the_volume(m1_ws):
    _reshred(m1_ws, "The Price Volume is limited to 3 pages.",
             "The Price Volume is limited to 3 pages. Resumes are limited to 2 pages each and do not "
             "count toward the Technical Volume page limit.")
    resume = " ".join(["word"] * 600)
    draft = ("# Volume I Technical\n" + " ".join(["word"] * 3500) + "\n\n# Resumes\n\n## Dana Okafor\n"
             f"{resume}\n\n## Second Resume\n{resume}\n")
    _write(m1_ws, T.DRAFT_PATH, draft)
    result = C.draft_within_limits(m1_ws, {})
    assert _passes(result) and "Volume I Technical: ~7.0/10 pages" in result["details"]
    _write(m1_ws, T.DRAFT_PATH, draft.replace(f"## Second Resume\n{resume}", "## Second Resume\n"
                                              + " ".join(["word"] * 1500)))
    result = C.draft_within_limits(m1_ws, {})
    assert result["passed"] is False and "Second Resume is 3 pages; limit 2" in result["details"]


def test_dates_match_source_reads_only_the_key_dates_section(m1_ws):
    internal = "\n## Internal schedule\n\n- Internal final draft needed: October 12, 2026\n"
    _write(m1_ws, C.BRIEF_PATH, BRIEF + internal)
    assert _passes(C.dates_match_source(m1_ws, {}))
    _write(m1_ws, C.BRIEF_PATH, BRIEF + "- Internal final draft needed: October 12, 2026\n")
    assert "2026-10-12" in C.dates_match_source(m1_ws, {})["details"]


def test_evidence_map_rejects_passages_unrelated_to_the_requirement(m2_ws):
    rows = _rows(m2_ws, T.EVIDENCE_MAP_PATH)
    for row in rows:
        if row["status"] == "mapped":
            row["passages"] = "key-personnel.md#p1"
    _save_rows(m2_ws, T.EVIDENCE_MAP_PATH, rows)
    result = C.evidence_map_complete(m2_ws, {})
    assert result["passed"] is False and "R-011: no mapped passage shares a term" in result["details"]


def test_uncited_fact_detector_catches_counts_magnitudes_awards_and_tables():
    text = ("## Profile\nLumen Fieldworks has launched 45 mobile apps. Our apps have been downloaded 2 million "
            "times. Lumen Fieldworks won the 2024 Transit Innovation Award.\n\n"
            "| Metric | Value |\n|---|---|\n| Monthly active riders | 250,000 |\n\n"
            "We will ship 4 updates a year. The app will support 3 languages.\n")
    flagged = [u["sentence"] for u in T.scan_grounding(text, {}, {})["uncited_claims"]]
    assert flagged == ["Lumen Fieldworks has launched 45 mobile apps.",
                       "Our apps have been downloaded 2 million times.",
                       "Lumen Fieldworks won the 2024 Transit Innovation Award.",
                       "Monthly active riders | 250,000"]


def test_kb_reads_csv_rows_and_reports_skipped_files(q_ws):
    _write(q_ws, "inputs/kb/prior-answers.csv", "Question,Answer\nDo you hold a SOC 2 Type II report?,"
                                                "Yes. SOC 2 Type II report issued March 2026.\n")
    (q_ws / "inputs" / "kb" / "policy.pdf").write_bytes(b"%PDF-1.4")
    passages = T.kb_passages(q_ws)
    assert passages["prior-answers.csv#p1"].startswith("Question: Do you hold a SOC 2 Type II report?; Answer:")
    out = T.kb_search(q_ws, query="SOC 2 report")
    assert out["results"][0]["passage_id"] == "prior-answers.csv#p1" and out["skipped_files"] == ["policy.pdf"]
    T.set_answer(q_ws, question_id="Q-004", status="answered", answer="Yes.",
                 citations=["prior-answers.csv#p1"])
    assert _passes(C.shred_complete(q_ws, {}))          # KB files are not solicitation sources


def test_docx_page_numbers_follow_rendered_and_section_breaks(tmp_path):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (f'<w:document {w}><w:body>'
            '<w:p><w:r><w:t>First page text shall be read.</w:t></w:r></w:p>'
            '<w:p><w:r><w:lastRenderedPageBreak/><w:t>Second page shall follow.</w:t></w:r></w:p>'
            '<w:p><w:r><w:br w:type="page"/></w:r><w:r><w:lastRenderedPageBreak/>'
            '<w:t>Third page shall not double count.</w:t></w:r></w:p>'
            '<w:p><w:pPr><w:sectPr><w:type w:val="nextPage"/></w:sectPr></w:pPr>'
            '<w:r><w:t>Still the third page shall end the section.</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Fourth page shall start the next section.</w:t></w:r></w:p>'
            '</w:body></w:document>')
    (tmp_path / "inputs").mkdir()
    with zipfile.ZipFile(tmp_path / "inputs" / "rfp.docx", "w") as zf:
        zf.writestr("word/document.xml", body)
    rows = T.shred_text(T.read_text(tmp_path, "inputs/rfp.docx"), "inputs/rfp.docx")
    assert [r["page"] for r in rows] == [1, 2, 3, 3, 4]


def test_milestone_hours_fit_one_run_of_the_limits():
    m = _manifest()
    for ms in m["milestones"]:
        assert ms["hours"][1] * m["estimate"]["usd_per_hour"] <= m["limits"]["max_usd"], ms["id"]
        assert ms["hours"][1] * 60 <= m["limits"]["max_wall_minutes"], ms["id"]


def test_a_volume_the_solicitation_exempts_is_not_outside_prose(m1_ws):
    draft = ("# Volume I Technical\n" + " ".join(["word"] * 3000) + "\n\n# Volume II Price\n"
             + " ".join(["word"] * 1500) + "\n")
    _reshred(m1_ws, "The Price Volume is limited to 3 pages.", "The Price Volume is not page limited.")
    _write(m1_ws, T.DRAFT_PATH, draft)
    assert _passes(C.draft_within_limits(m1_ws, {}))
    _reshred(m1_ws, "The Price Volume is not page limited.", "The Price Volume follows the pricing template.")
    result = C.draft_within_limits(m1_ws, {})
    assert result["passed"] is False and "3 pages of prose sit outside" in result["details"]


def test_questionnaire_needs_every_csv_but_not_its_cover_instructions(q_ws):
    _write(q_ws, "inputs/instructions.md", "Return the completed questionnaire by email.\n")
    assert _passes(C.shred_complete(q_ws, {}))
    _write(q_ws, "inputs/privacy-questionnaire.csv", "ID,Question\nP-1,Do you sell personal data?\n")
    details = C.shred_complete(q_ws, {})["details"]
    assert "inputs/privacy-questionnaire.csv is not shredded" in details


def test_malformed_inputs_fail_checks_instead_of_crashing(m1_ws):
    _write(m1_ws, "work/ragged.csv", "a,b\n1,2,3,4\n")
    assert T.read_csv_rows(m1_ws, "work/ragged.csv") == [{"a": "1", "b": "2", "": "3,4"}]
    _edit_requirements(m1_ws, lambda d: d["requirements"].append("R-099"))
    for name, fn in C.CHECK_DEFS.items():
        result = fn(m1_ws, {})
        assert result["passed"] is False and "string id" in result["details"], name


def test_a_passage_denying_a_certification_does_not_ground_a_yes(q_ws):
    _write(q_ws, "inputs/kb/attestations.md", "# Attestations\n\nLumen Fieldworks is not FedRAMP authorized "
           "and is pursuing a SOC 2 Type II report. Its penetration test had no findings in its HIPAA scope.\n")
    for qid, answer in (("Q-005", "Yes."), ("Q-004", "Yes, we hold a SOC 2 Type II report.")):
        with pytest.raises(ToolError, match="affirms|not in the cited"):
            T.set_answer(q_ws, question_id=qid, status="answered", answer=answer,
                         citations=["attestations.md#p1"])
    T.set_answer(q_ws, question_id="Q-005", status="answered",
                 answer="No. Lumen Fieldworks is not FedRAMP authorized.", citations=["attestations.md#p1"])
    assert C.questionnaire_answers_grounded(q_ws, {})["details"].startswith("1 answered")
    passages = T.kb_passages(q_ws)
    scan = T.scan_grounding("## S\nLumen Fieldworks is FedRAMP authorized [KB:attestations.md#p1]. "
                            "It had no findings in its HIPAA scope [KB:attestations.md#p1].\n", passages, {})
    assert [u["not_in_cited_text"] for u in scan["unsupported"]] == [["FedRAMP"]]
