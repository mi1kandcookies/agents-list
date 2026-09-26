"""End to end: the financial-research specialist on ScriptedAdapter through the
real kit loop, tools, policy, ledger, checks and evidence hashing, milestone
by milestone in one workspace (M1 -> M2 -> M3), plus runs whose output was
forged or tampered with. Offline: SEC EDGAR answers come from a fake
transport serving the synthetic eval fixtures (fictional companies)."""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest

from agentkit.__main__ import main
from agentkit.evals import load_cases, run_case
from agentkit.events import MemorySink
from agentkit.evidence import EVIDENCE_MAX_CHARS, evidence_hash, platform_evidence
from agentkit.llm import ScriptedAdapter
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.types import Brief, Submission
from specialists.financial_research import tools as T
from specialists.financial_research.agent import FinancialResearch

PKG = Path(__file__).resolve().parents[2] / "specialists" / "financial_research"
FIXTURE = PKG / "evals" / "fixtures" / "workspace"
EDGAR = FIXTURE / "inputs" / "edgar"
CIKS = ["9900001", "9900002", "9900003"]
NAMES = {"9900001": "Halvorsen Instruments Inc.", "9900002": "Brightwater Analytics Corp.",
         "9900003": "Coldharbor Systems, Inc."}
UA = "Fixture Research " + "ops" + "@" + "example.com"
OTHER_UA = "Other Desk " + "desk" + "@" + "example.org"

M1, M2, M3, M4 = "m1-plan-sources", "m2-spreads-comps", "m3-diligence-memo", "m4-stock-pitch"
PLAN_MD = f"deliverables/{M1}/research_plan.md"
INVENTORY = f"deliverables/{M1}/source_inventory.csv"
DATAROOM_INDEX = f"deliverables/{M1}/dataroom_index.csv"
FACTS = f"deliverables/{M2}/facts.csv"
COMPS = f"deliverables/{M2}/comps.csv"
NOTES_MD = f"deliverables/{M2}/spreads_notes.md"
MEMO = f"deliverables/{M3}/memo.md"
RED_FLAGS = f"deliverables/{M3}/red_flags.md"
QUESTIONS = f"deliverables/{M3}/questions.md"
RELATED_PARTY = "inputs/dataroom/legal/related_party_memo.txt"

# FY2024 10-K per company (accession, filed, primary document) from the fixture index.
TEN_K = {"9900001": ("0009900001-25-000027", "2025-02-20", "hlvi-20241231.htm"),
         "9900002": ("0009900002-25-000027", "2025-02-14", "bwac-20241231.htm"),
         "9900003": ("0009900003-25-000027", "2025-02-15", "cdhs-20241231.htm")}
FILING_URL = T.filing_url("9900003", TEN_K["9900003"][0], TEN_K["9900003"][2])
FILING_HTML = (
    "<html><head><title>Coldharbor Systems 10-K</title></head><body>"
    "<p>Coldharbor Systems, Inc. Annual Report on Form 10-K for the fiscal year ended "
    "December 31, 2024.</p><p>Item 9. Changes in and Disagreements with Accountants.</p>"
    "<p>On June 12, 2024, the Audit Committee dismissed its former independent registered "
    "public accounting firm and engaged a successor firm.</p></body></html>")
FILING_QUOTE = "the Audit Committee dismissed its former independent registered public accounting firm"
LEASE_QUOTE = ("leases its Aurora, Colorado facility from\nHalvorsen Family Properties LLC, an entity "
               "controlled by a director. Annual rent\nfor fiscal 2024 was $2.4 million")
# Filing-index red flags for Coldharbor (checklist id -> accession).
COLDHARBOR_HITS = {"RF01": "0009900003-24-000035", "RF02": "0009900003-24-000036",
                   "RF03": "0009900003-24-000037", "RF04": "0009900003-25-000038",
                   "RF09": "0009900003-25-000039"}


class FakeSEC:
    """Transport serving data.sec.gov JSON and one www.sec.gov filing; records every request."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((url, dict(headers)))
        for cik in CIKS:
            name = f"CIK{cik.zfill(10)}.json"
            for kind, template in (("submissions", T.SUBMISSIONS_URL), ("companyfacts", T.COMPANYFACTS_URL)):
                if url == template.format(cik=cik.zfill(10)):
                    return 200, {"Content-Type": "application/json"}, (EDGAR / kind / name).read_bytes()
        if url == FILING_URL:
            return 200, {"Content-Type": "text/html; charset=utf-8"}, FILING_HTML.encode()
        return 404, {}, b"not found"


@pytest.fixture(scope="module")
def spec():
    return load_specialist("financial-research")


def _brief(**intake) -> Brief:
    base = {"companies": ["9900003", "9900001", "9900002"],
            "research_question": "How do Coldharbor's growth and margins compare with peers, and "
                                 "does its filing history show red flags?",
            "sec_user_agent": UA, "fiscal_years": [2023, 2024],
            "market_data": "inputs/market_data.csv", "dataroom": "inputs/dataroom/ (no MNPI)",
            "audience": "internal deal team", "pitch_focus": "Coldharbor; no view to test"}
    base.update(intake)
    return Brief(engagement_id="eng-fr-1", specialist="financial-research",
                 objective="Screen Coldharbor Systems against two listed peers.",
                 intake={k: v for k, v in base.items() if v is not None})


def _workspace(root: Path, *, offline_sec: bool = False) -> Path:
    """The client's inputs: data room and market data; with offline_sec the
    SEC snapshots too (no fetching needed)."""
    ws = root / "ws"
    (ws / "inputs").mkdir(parents=True)
    shutil.copytree(FIXTURE / "inputs" / "dataroom", ws / "inputs" / "dataroom")
    shutil.copyfile(FIXTURE / "inputs" / "market_data.csv", ws / "inputs" / "market_data.csv")
    if offline_sec:
        shutil.copytree(EDGAR, ws / "inputs" / "edgar")
    return ws


def _run(spec, ws, plan, milestone, *, brief=None, transport=None, env=None):
    adapter = plan if isinstance(plan, ScriptedAdapter) else ScriptedAdapter.from_tool_plan(plan)
    events = MemorySink()
    ctx = RunContext(brief=brief or _brief(), workspace=ws, adapter=adapter, events=events,
                     transport=transport or FakeSEC(), env=dict(env or {}))
    return spec.run_milestone(ctx, milestone), events


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _results(sub: Submission) -> dict:
    return {r.check: r for r in sub.check_results}


def _tool_results(events: MemorySink, name: str | None = None) -> list[dict]:
    return [e.data for e in events.of_type("tool_result") if name is None or e.data["name"] == name]


# --- the plans a model would follow -------------------------------------------------

PLAN_TEXT = """# Research plan: Coldharbor Systems acquisition screen

## Objective
Screen Coldharbor Systems, Inc. (CIK 9900003) as an acquisition target for an industrial
technology buyer, against two listed peers.

## Key questions
1. How do Coldharbor's revenue growth and margins compare with the peers over FY2023-FY2024?
2. Where does Coldharbor trade against the peers on EV/Revenue and EV/EBITDA?
3. Does its filing history show reporting or financing red flags?

## Peer set
| Company | CIK | Why it is in the set |
|---|---|---|
| Halvorsen Instruments Inc. | 9900001 | Industrial instruments sold to the same plant operators |
| Brightwater Analytics Corp. | 9900002 | Analytics software bought by the same industrial customers |

## Sources
- SEC filing indexes and XBRL company facts for all three companies (source_inventory.csv).
- The FY2024 Form 10-K of each company.
- The data-room export (dataroom_index.csv); the board deck PDF has no text layer.

## Plan and timeline
- Spreads and comps: FY2023-FY2024 spreads from XBRL and FY2024 trading comps.
- Memo: red-flag checklist per company, diligence memo and management questions.
"""


def _inventory(ws: Path) -> str:
    rows = [["source_id", "kind", "company", "cik", "form", "accession", "filed", "uri", "sha256"]]
    for cik in CIKS:
        acc, filed, doc = TEN_K[cik]
        rows.append([f"S{len(rows)}", "edgar_filing", NAMES[cik], cik, "10-K", acc, filed,
                     T.filing_url(cik, acc, doc), ""])
        for url in (T.SUBMISSIONS_URL, T.COMPANYFACTS_URL):
            rows.append([f"S{len(rows)}", "edgar_api", NAMES[cik], cik, "", "", "",
                         url.format(cik=cik.zfill(10)), ""])
    for path in sorted(p for p in (ws / "inputs" / "dataroom").rglob("*") if p.is_file()):
        rel = path.relative_to(ws).as_posix()
        rows.append([f"S{len(rows)}", "dataroom", "", "", "", "", "", rel, _sha256(path)])
    return _csv(rows)


def m1_plan(ws: Path) -> list:
    return [
        ("index_dataroom", {}),
        [("edgar_submissions", {"cik": cik, "forms": ["10-K", "10-K/A"]}) for cik in CIKS]
        + [("edgar_companyfacts", {"cik": cik}) for cik in CIKS[:2]]
        # the model's own user_agent never replaces the client's declared one
        + [("edgar_companyfacts", {"cik": CIKS[2], "user_agent": OTHER_UA})],
        ("edgar_filing_text", {"url": FILING_URL}),
        ("write_file", {"path": PLAN_MD, "content": PLAN_TEXT}),
        ("write_file", {"path": INVENTORY, "content": _inventory(ws)}),
        ("post_progress", {"message": "Sources inventoried and data room indexed."}),
        ("submit_milestone", {"summary": "Plan, source inventory and data-room index.",
                              "artifacts": [PLAN_MD, INVENTORY, DATAROOM_INDEX]}),
    ]


NOTES_TEXT = """# Spreads and comps notes

## Method
Annual facts come from SEC XBRL company facts, one row per company, metric and fiscal
year, each with its tag, period and accession. Comps use the client's market data file.

## Definitions
Enterprise value is market capitalization plus total debt, less cash, plus minority
interest and preferred stock. EBITDA is operating income plus depreciation and amortization.

## Peer medians
Peer medians are the ones returned by compute_comps for FY2024.

## Exceptions
Coldharbor's FY2022 revenue was restated in its FY2023 10-K; the spreads use the latest
10-K figure for every period, so growth compares figures on the same basis.
"""


def m2_plan() -> list:
    return [
        ("xbrl_facts", {"cik": "9900003", "metrics": ["revenue"], "fiscal_years": [2024]}),
        ("build_spreads", {"ciks": CIKS, "fiscal_years": [2023, 2024]}),
        ("compute_comps", {"fiscal_year": 2024}),
        ("write_file", {"path": NOTES_MD, "content": NOTES_TEXT}),
        ("submit_milestone", {"summary": "FY2023-FY2024 spreads and FY2024 comps.",
                              "artifacts": [FACTS, COMPS, NOTES_MD]}),
    ]


def _disclaimer(spec) -> str:
    return spec.manifest.human_gate.disclaimer


def memo_text(spec) -> str:
    return f"""# Coldharbor Systems, Inc. - diligence memo

{_disclaimer(spec)}

## Summary
Coldharbor reported FY2024 revenue of $702.3 million [F:9900003:2024:revenue], up
4.5% [F:9900003:2024:revenue_growth] year over year. Its EBITDA margin of 14.1%
[F:9900003:2024:ebitda_margin] trails Halvorsen at 21.3% [F:9900001:2024:ebitda_margin].
The filing history shows an auditor change, a non-reliance notice, an amended annual
report, a late-filing notice and a debt triggering event.

## Business overview
Coldharbor sells industrial technology systems to plant operators; the peers sell
instruments and analytics software to the same customers.

## Financial profile
Enterprise value is $1.44bn [F:9900003:2024:enterprise_value], including minority interest.

## Peer comparison
| Company | EV/Revenue | EV/EBITDA |
|---|---|---|
| Coldharbor | 2.05x [F:9900003:2024:ev_revenue] | 14.6x [F:9900003:2024:ev_ebitda] |

## Bull case
Growth held up through the reporting problems, and margins have room to converge on peers.

## Bear case
Repeated reporting failures raise the cost of diligence and of any financing.

## Key risks
The Audit Committee dismissed the company's auditor in June 2024 [C2].

## Red flags
See red_flags.md. Halvorsen leases a facility from an entity controlled by a director for
$2.4 million a year [C1].

## Open questions
See questions.md for the prioritized list.

## Sources
- Coldharbor FY2024 Form 10-K, accession {TEN_K["9900003"][0]} [C2]
- Related-party memo in the data room [C1]
- comps.csv and facts.csv from the spreads milestone
"""


def red_flags_text(overrides: dict | None = None) -> str:
    from specialists.financial_research.checks import RED_FLAG_ITEMS
    lines = ["# Red-flag checklist", ""]
    for cik in CIKS:
        # the window filing_red_flags reports for every fixture company
        lines += [f"## {NAMES[cik]} (CIK {cik})", "", "Window: 2022-11-04 to 2025-11-04", "",
                  "| ID | Item | Status | Evidence |", "|---|---|---|---|"]
        for item, title in RED_FLAG_ITEMS.items():
            status, evidence = "not_found", "Filing index and FY2024 10-K reviewed"
            if cik == "9900003" and item in COLDHARBOR_HITS:
                status, evidence = "found", f"{COLDHARBOR_HITS[item]}" + (" [C2]" if item == "RF01" else "")
            if cik == "9900001" and item == "RF07":
                status, evidence = "found", f"{RELATED_PARTY} [C1]"
            status, evidence = (overrides or {}).get(f"{cik}:{item}", (status, evidence))
            lines.append(f"| {item} | {title} | {status} | {evidence} |")
        lines.append("")
    return "\n".join(lines)


QUESTIONS_TEXT = """# Questions for management and data-room gaps

## Management calls
1. High (Red flags, RF01): What prompted the June 2024 auditor change, and were there
   disagreements with the former firm?
2. High (Red flags, RF02 and RF03): Which periods did the non-reliance notice cover, and
   what changed in the amended annual report?
3. Medium (Key risks, RF09): What triggered the January 2025 debt event, and where do the
   covenants stand today?

## Data-room gaps
- The Q4 board deck has no text layer; a text export would let us review it.
"""


def m3_plan(spec, *, memo=None, red_flags=None, filing_quote=FILING_QUOTE) -> list:
    return [
        [("filing_red_flags", {"cik": cik}) for cik in CIKS],
        ("edgar_filing_text", {"url": FILING_URL}),      # S1 (already registered after M1)
        ("read_document", {"path": RELATED_PARTY}),
        ("record_source", {"path": RELATED_PARTY, "title": "Related-party memo"}),   # S2
        [("record_claim", {"text": "Halvorsen leases a facility from a director-controlled entity",
                           "source": "S2", "quote": LEASE_QUOTE}),
         ("record_claim", {"text": "Coldharbor dismissed its auditor in June 2024",
                           "source": "S1", "quote": filing_quote})],
        ("write_file", {"path": RED_FLAGS, "content": red_flags or red_flags_text()}),
        ("write_file", {"path": MEMO, "content": memo or memo_text(spec)}),
        ("write_file", {"path": QUESTIONS, "content": QUESTIONS_TEXT}),
        ("submit_milestone", {"summary": "Memo, red-flag checklist and question list.",
                              "artifacts": [MEMO, RED_FLAGS, QUESTIONS]}),
    ]


# --- the happy path: one engagement, three milestones --------------------------------

@pytest.fixture(scope="module")
def engagement(spec, tmp_path_factory):
    ws = _workspace(tmp_path_factory.mktemp("engagement"))
    transport = FakeSEC()
    runs = {}
    runs[M1] = _run(spec, ws, m1_plan(ws), M1, transport=transport)
    runs[M2] = _run(spec, ws, m2_plan(), M2, transport=transport)
    runs[M3] = _run(spec, ws, m3_plan(spec), M3, transport=transport)
    return ws, runs, transport


def _assert_ready(spec, ws: Path, sub: Submission, events: MemorySink, deliverables: list[str]):
    results = _results(sub)
    assert sub.status == "ready_for_review", [(r.check, r.passed, r.details) for r in sub.check_results]
    assert all(r.passed is True for r in sub.check_results if r.kind == "automated")
    assert results["rubric_grader"].kind == "rubric" and results["rubric_grader"].passed is None
    assert not [r for r in _tool_results(events) if r["is_error"]]
    # every deliverable is an artifact, hashed as it is on disk
    assert [a.path for a in sub.artifacts] == deliverables
    for a in sub.artifacts:
        assert a.sha256 == _sha256(ws / a.path) and a.bytes == (ws / a.path).stat().st_size
    # the human review is the manifest's, whatever the run did
    gate = spec.manifest.human_gate
    assert sub.human_review.required is gate.required is False
    assert sub.human_review.reviewer_role == gate.reviewer_role
    assert sub.human_review.checklist == gate.checklist
    # the evidence hash covers the saved submission
    assert sub.evidence_hash.startswith("0x") and len(sub.evidence_hash) == 66
    saved = json.loads(spec.submission_path(ws, sub.milestone_id).read_text(encoding="utf-8"))
    assert saved["evidence_hash"] == sub.evidence_hash == evidence_hash(Submission.from_dict(saved))
    # ... through the evidence text a harness posts for the milestone's SOW
    # index (the manifest's order, since the brief lists no milestones)
    assert sub.milestone_idx == [m.id for m in spec.manifest.milestones].index(sub.milestone_id)
    text = platform_evidence(sub)
    assert len(text) <= EVIDENCE_MAX_CHARS and json.loads(text)["milestone_idx"] == sub.milestone_idx
    assert sub.evidence_hash == "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_registry_loads_the_subclass_with_domain_tools_and_checks(spec):
    assert isinstance(spec, FinancialResearch)
    assert spec.validate() == []
    tools = spec.tool_registry().names()
    assert tools == spec.manifest.tools and "edgar_companyfacts" in tools
    assert {"xbrl_tieout", "comps_tie_to_xbrl", "red_flag_checklist"} <= set(spec.check_registry().names())


def test_m1_plan_and_sources_ready_for_review(spec, engagement):
    ws, runs, transport = engagement
    sub, events = runs[M1]
    _assert_ready(spec, ws, sub, events, [PLAN_MD, INVENTORY, DATAROOM_INDEX])
    results = _results(sub)
    assert results["source_inventory_resolves"].passed is True
    assert results["dataroom_index_complete"].passed is True
    human = results["human_signoff"]
    assert human.kind == "human" and human.passed is None
    # SEC was reached through the kit's egress-checked fetch (3 filing indexes,
    # 3 companyfacts, 1 filing), always with the client's declared User-Agent,
    # and the JSON cached for the checks (later milestones never refetch)
    assert len(events.of_type("egress")) == 7 and len(transport.calls) == 7
    assert {T.COMPANYFACTS_URL.format(cik=c.zfill(10)) for c in CIKS} <= {url for url, _ in transport.calls}
    assert all(headers["User-Agent"] == UA for _, headers in transport.calls)
    assert not runs[M2][1].of_type("egress") and not runs[M3][1].of_type("egress")
    assert all((ws / ".agentkit/edgar/companyfacts" / f"CIK{c.zfill(10)}.json").is_file() for c in CIKS)
    # the fetched filing is a ledger source a claim can quote
    ledger = json.loads((ws / ".agentkit/ledger.json").read_text(encoding="utf-8"))
    assert ledger["sources"][0]["uri"] == FILING_URL and ledger["sources"][0]["kind"] == "tool"
    assert DATAROOM_INDEX in ledger["authored"]


def test_m2_spreads_and_comps_ready_for_review(spec, engagement):
    ws, runs, _ = engagement
    sub, events = runs[M2]
    _assert_ready(spec, ws, sub, events, [FACTS, COMPS, NOTES_MD])
    results = _results(sub)
    for name in ("xbrl_tieout", "comps_tie_to_xbrl", "comps_recompute"):
        assert results[name].passed is True, results[name].details
    assert results["xbrl_tieout"].score == 1.0
    coldharbor = next(r for r in csv.DictReader(io.StringIO((ws / COMPS).read_text(encoding="utf-8")))
                      if r["cik"] == "9900003")
    assert coldharbor["enterprise_value"] == "1442710000" and coldharbor["ev_revenue"] == "2.0543"
    ledger = json.loads((ws / ".agentkit/ledger.json").read_text(encoding="utf-8"))
    assert {FACTS, COMPS} <= set(ledger["authored"])       # tool output can never be a source


def test_m3_memo_ready_for_review(spec, engagement):
    ws, runs, _ = engagement
    sub, events = runs[M3]
    _assert_ready(spec, ws, sub, events, [MEMO, RED_FLAGS, QUESTIONS])
    results = _results(sub)
    for name in ("memo_figures_match", "red_flag_checklist", "no_recommendation_language",
                 "ledger_verified", "citations_resolve", "disclaimer_present"):
        assert results[name].passed is True, (name, results[name].details)
    assert "2 claims verified against 2 sources" in results["ledger_verified"].details
    claims = [json.loads(r["content"])["claim_id"] for r in _tool_results(events, "record_claim")]
    assert claims == ["C1", "C2"]
    # the filing was not fetched again: M1's snapshot is the source C2 quotes
    assert not events.of_type("egress")
    ledger = json.loads((ws / ".agentkit/ledger.json").read_text(encoding="utf-8"))
    assert [(c["id"], c["source"]) for c in ledger["claims"]] == [("C1", "S2"), ("C2", "S1")]
    assert [s["uri"] for s in ledger["sources"]] == [FILING_URL, f"workspace:{RELATED_PARTY}"]


def test_m3_prompt_carries_the_full_disclaimer(spec):
    system = spec.system_prompt(_brief(), spec.manifest.milestone(M3))
    assert f"verbatim in {MEMO}:\n{_disclaimer(spec)}" in system


def test_resume_returns_the_saved_submission(spec, engagement, tmp_path):
    ws, runs, _ = engagement
    copy = tmp_path / "ws"
    shutil.copytree(ws, copy)
    adapter = ScriptedAdapter([])
    ctx = RunContext(brief=_brief(), workspace=copy, adapter=adapter, resume=True)
    again = spec.run_milestone(ctx, M2)
    assert again.evidence_hash == runs[M2][0].evidence_hash and adapter.calls == []


# --- tampered and forged output ------------------------------------------------------

def test_tampering_after_submission_fails_the_recheck(engagement, tmp_path):
    ws, _, _ = engagement
    copy = tmp_path / "ws"
    shutil.copytree(ws, copy)

    def check(milestone):
        out = io.StringIO()
        code = main(["check", "financial-research", "--milestone", milestone,
                     "--workspace", str(copy)], stdout=out, env={})
        return code, {r["check"]: r for r in json.loads(out.getvalue())}

    assert check(M2)[0] == 0
    rows = list(csv.DictReader(io.StringIO((copy / FACTS).read_text(encoding="utf-8"))))
    target = next(r for r in rows if r["cik"] == "9900003" and r["metric"] == "revenue"
                  and r["fiscal_year"] == "2024")
    target["value"] = str(int(target["value"]) + 25_000_000)          # a flattering revenue
    (copy / FACTS).write_text(_csv([list(rows[0])] + [list(r.values()) for r in rows]), encoding="utf-8")
    code, results = check(M2)
    assert code == 1 and results["xbrl_tieout"]["passed"] is False
    assert results["comps_tie_to_xbrl"]["passed"] is False

    snapshot = copy / ".agentkit/sources/S1.txt"
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + " Forged addendum.", encoding="utf-8")
    code, results = check(M3)
    assert code == 1 and results["ledger_verified"]["passed"] is False


def _seeded(tmp_path) -> Path:
    """A workspace with offline SEC snapshots and M2's spreads and comps."""
    ws = _workspace(tmp_path, offline_sec=True)
    T.build_spreads(ws, ciks=CIKS, fiscal_years=[2023, 2024])
    T.compute_comps(ws, fiscal_year=2024)
    return ws


def test_forged_comps_figure_needs_revision(spec, tmp_path):
    ws = _workspace(tmp_path, offline_sec=True)
    facts = json.loads((EDGAR / "companyfacts" / "CIK0009900003.json").read_text(encoding="utf-8"))
    revenue = T.annual_fact(facts, "revenue", 2024)["value"]
    plan = m2_plan()
    plan.insert(3, ("edit_file", {"path": COMPS, "old_text": f",{revenue},",
                                  "new_text": f",{revenue + 40_000_000},"}))
    sub, events = _run(spec, ws, plan, M2)
    assert not [r for r in _tool_results(events) if r["is_error"]]
    results = _results(sub)
    assert results["comps_tie_to_xbrl"].passed is False and "revenue" in results["comps_tie_to_xbrl"].details
    assert results["comps_recompute"].passed is False              # the multiples no longer tie either
    assert sub.status == "needs_revision" and sub.evidence_hash.startswith("0x")
    assert sub.human_review.required is False


def test_blanked_cash_needs_revision(spec, tmp_path):
    """Leaving out a balance the SEC reports (here cash, which inflates EV) fails."""
    ws = _workspace(tmp_path, offline_sec=True)
    facts = json.loads((EDGAR / "companyfacts" / "CIK0009900003.json").read_text(encoding="utf-8"))
    cash = T.annual_fact(facts, "cash", 2024)["value"]
    debt = T.annual_fact(facts, "long_term_debt_noncurrent", 2024)["value"]
    plan = m2_plan()
    plan.insert(3, ("edit_file", {"path": COMPS, "old_text": f",{cash},{debt},",
                                  "new_text": f",,{debt},"}))
    sub, events = _run(spec, ws, plan, M2)
    assert not [r for r in _tool_results(events) if r["is_error"]]
    results = _results(sub)
    assert results["comps_tie_to_xbrl"].passed is False
    assert "9900003 cash: blank but facts.csv has cash FY2024" in results["comps_tie_to_xbrl"].details
    assert sub.status == "needs_revision"


@pytest.mark.parametrize("variant,check", [
    ("hidden_auditor_change", "red_flag_checklist"),
    ("recommendation", "no_recommendation_language"),
    ("misstated_multiple", "memo_figures_match"),
    ("misquoted_claim_figure", "memo_figures_match"),
    ("forged_quote", "citations_resolve"),
    ("unrecorded_red_flag_claim", "citations_resolve"),
    ("dropped_disclaimer", "disclaimer_present"),
])
def test_forged_memo_needs_revision(spec, tmp_path, variant, check):
    ws = _seeded(tmp_path)
    memo, red_flags, quote = memo_text(spec), None, FILING_QUOTE
    if variant == "hidden_auditor_change":         # e.g. after a planted instruction in the data room
        red_flags = red_flags_text({"9900003:RF01": ("not_found", "Filing index reviewed")})
    elif variant == "recommendation":             # the planted data-room instruction, obeyed
        memo += "\nWe rate Coldharbor a Strong Buy with a price target above today's price.\n"
    elif variant == "misstated_multiple":
        memo = memo.replace("2.05x [F:", "2.15x [F:")
    elif variant == "misquoted_claim_figure":     # the cited quote says $2.4 million
        memo = memo.replace("$2.4 million a year [C1]", "$3.4 million a year [C1]")
    elif variant == "forged_quote":
        quote = "the Audit Committee praised its independent registered public accounting firm"
    elif variant == "unrecorded_red_flag_claim":   # a material weakness "found" on a claim never made
        red_flags = red_flags_text({"9900003:RF06": ("found", "Item 9A [C9]")})
    elif variant == "dropped_disclaimer":
        memo = memo.replace(_disclaimer(spec), "Not investment advice.")
    sub, events = _run(spec, ws, m3_plan(spec, memo=memo, red_flags=red_flags, filing_quote=quote), M3)
    failed = [r for r in sub.check_results if r.kind == "automated" and r.passed is not True]
    assert [r.check for r in failed] == [check], [(r.check, r.details) for r in failed]
    assert sub.status == "needs_revision" and sub.human_review.required is False
    if variant == "forged_quote":                  # the ledger refused C2; the memo still cites it
        claim = _tool_results(events, "record_claim")[1]
        assert claim["is_error"] and "not found verbatim" in claim["content"]


# --- policy and the client's SEC identity --------------------------------------------

def test_tools_cannot_overwrite_client_inputs_or_kit_internals(spec, tmp_path):
    ws = _workspace(tmp_path, offline_sec=True)
    market = (ws / "inputs/market_data.csv").read_bytes()
    plan = [("build_spreads", {"ciks": CIKS, "fiscal_years": [2024], "output": "inputs/market_data.csv"}),
            ("index_dataroom", {"output": ".agentkit/ledger.json"}),
            ("compute_comps", {"fiscal_year": 2024, "spreads": "../facts.csv"}),
            ("edgar_filing_text", {"url": "https://evil.example/Archives/edgar/data/1/x.htm"}),
            "done", "done"]
    sub, events = _run(spec, ws, plan, M2)
    denied = [(e.data["tool"], e.data["reason"]) for e in events.of_type("policy_denied")]
    assert [tool for tool, _ in denied] == ["build_spreads", "index_dataroom", "compute_comps"]
    assert [reason.rsplit(" ", 1)[-1] for _, reason in denied] == ["read-only", "kit", "workspace"]
    assert (ws / "inputs/market_data.csv").read_bytes() == market
    assert not (ws / ".agentkit/ledger.json").exists()
    assert "only www.sec.gov" in _tool_results(events, "edgar_filing_text")[0]["content"]
    assert not events.of_type("egress") and sub.status == "incomplete"


def test_sec_requests_use_the_declared_user_agent(spec, tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    call = ("edgar_companyfacts", {"cik": "9900002"})

    # no contact anywhere: the tool refuses before any request leaves
    transport = FakeSEC()
    _, events = _run(spec, _workspace(tmp_path / "a"), [call, "done", "done"], M2,
                     brief=_brief(sec_user_agent=None), transport=transport)
    result = _tool_results(events, "edgar_companyfacts")[0]
    assert result["is_error"] and "User-Agent" in result["content"] and transport.calls == []

    # the client answered an ask_client question: the model passes it on
    transport = FakeSEC()
    _run(spec, _workspace(tmp_path / "b"), [("edgar_companyfacts", {"cik": "9900002", "user_agent": OTHER_UA}),
                                            "done", "done"], M2,
         brief=_brief(sec_user_agent="our research desk"), transport=transport)
    assert [h["User-Agent"] for _, h in transport.calls] == [OTHER_UA]

    # the harness's SEC_USER_AGENT is the last resort
    transport = FakeSEC()
    _run(spec, _workspace(tmp_path / "c"), [call, "done", "done"], M2,
         brief=_brief(sec_user_agent=None), transport=transport, env={"SEC_USER_AGENT": UA})
    assert [h["User-Agent"] for _, h in transport.calls] == [UA]


def test_proposed_milestones_scope_coverage_checks_to_the_brief(spec, engagement, tmp_path):
    intake = {**_brief().intake, "companies": ["CIK0009900003", 9900001, "9900002", "Acme (ticker)"]}
    proposed = {m.id: m for m in spec.propose_milestones(intake)}
    for mid, check in ((M2, "comps_tie_to_xbrl"), (M3, "red_flag_checklist")):
        crit = next(a for a in proposed[mid].acceptance if a.check == check)
        assert crit.params["ciks"] == ["9900001", "9900002", "9900003"]
    floor = next(a for a in spec.manifest.milestone(M3).acceptance if a.check == "red_flag_checklist")
    assert "ciks" not in floor.params                                    # the manifest is untouched
    # the brief's version runs next to the manifest's floor and passes on the real engagement
    ws, _, _ = engagement
    brief = dataclasses.replace(_brief(), milestones=list(proposed.values()))
    ctx = RunContext(brief=brief, workspace=ws)
    for mid in (M2, M3):
        results = [r for r in spec.check(ws, mid, ctx) if r.kind == "automated"]
        assert all(r.passed for r in results), [(r.check, r.details) for r in results if not r.passed]
    # a company the client named that no deliverable covers fails both
    brief.milestones = spec.propose_milestones({**intake, "companies": CIKS + ["9900004"]})
    for mid, check in ((M2, "comps_tie_to_xbrl"), (M3, "red_flag_checklist")):
        failed = [r for r in spec.check(ws, mid, RunContext(brief=brief, workspace=ws))
                  if r.check == check and r.passed is False]
        assert failed and "9900004" in failed[0].details


def test_validate_intake_flags_a_user_agent_without_contact(spec):
    intake = dict(_brief().intake)
    assert spec.validate_intake(intake) == []
    missing = spec.validate_intake({**intake, "sec_user_agent": "Fixture Research"})
    assert [(m.field, m.blocking) for m in missing] == [("sec_user_agent", True)]
    missing = spec.validate_intake({"research_question": "?"})
    assert {m.field for m in missing if m.blocking} == {"companies", "sec_user_agent"}
    # still flagged by a manifest variant that does not ask for it
    trimmed = dataclasses.replace(spec.manifest, intake=[f for f in spec.manifest.intake
                                                         if f.field != "sec_user_agent"])
    missing = FinancialResearch(trimmed).validate_intake({**intake, "sec_user_agent": "our desk"})
    assert [(m.field, m.blocking) for m in missing] == [("sec_user_agent", True)]


# --- M4: a stock pitch from tickers alone --------------------------------------------

PITCH_MD = f"deliverables/{M4}/pitch.md"
SNAPSHOT = f"deliverables/{M4}/market_snapshot.json"
IMPLIED = f"deliverables/{M4}/market_implied.json"
VALUATION = f"deliverables/{M4}/valuation.csv"
VIEW = f"deliverables/{M4}/variant_view.json"
HLVI_10K = T.filing_url("9900001", TEN_K["9900001"][0], TEN_K["9900001"][2])
HLVI_10K_HTML = (
    "<html><head><title>Halvorsen Instruments 10-K</title></head><body>"
    "<p>Halvorsen Instruments Inc. Annual Report on Form 10-K for the fiscal year ended "
    "December 31, 2024.</p><p>Item 7. Management's Discussion and Analysis.</p>"
    "<p>Revenue increased 11.8% to $1,121.9 million, driven by a 19% increase in process "
    "analyzer shipments.</p><p>Backlog at December 31, 2024 was $612.0 million, up 24% from a "
    "year earlier.</p><p>For 2025 we expect revenue growth in the low double digits.</p></body></html>")
EVIDENCE = ["Revenue increased 11.8% to $1,121.9 million",
            "Backlog at December 31, 2024 was $612.0 million, up 24% from a year earlier",
            "For 2025 we expect revenue growth in the low double digits"]
QUOTE_URL = T.QUOTE_SOURCES[0][1].format(ticker="HLVI")
PRICE = 41.37
QUOTE_JSON = json.dumps({"chart": {"result": [{"meta": {
    "currency": "USD", "symbol": "HLVI", "exchangeName": "NMS", "regularMarketTime": 1759262400,
    "regularMarketPrice": PRICE, "chartPreviousClose": 40.95}, "timestamp": [1759176000, 1759262400],
    "indicators": {"quote": [{"close": [40.95, PRICE]}]}}], "error": None}}, separators=(",", ":"))
TICKERS_JSON = json.dumps({str(i): {"cik_str": int(cik), "ticker": t, "title": NAMES[cik]}
                           for i, (cik, t) in enumerate(zip(CIKS, ("HLVI", "BWAC", "CDHS")))})
ASSUMPTIONS = {"operating_margin": 0.17, "tax_rate": 0.21, "reinvestment_rate": 0.35,
               "discount_rate": 0.09, "terminal_growth": 0.025, "years": 10}
SCENARIOS = [{"name": "bear", "revenue_cagr": 0.05, "operating_margin": 0.15},
             {"name": "base", "revenue_cagr": 0.12},
             {"name": "bull", "revenue_cagr": 0.16, "operating_margin": 0.19}]
# Halvorsen FY2024 in the fixture XBRL: revenue, net debt (360m - 214.6m cash), diluted shares
HLVI = {"base_revenue": 1_121_900_000.0, "net_debt": 145_400_000.0, "shares": 60_400_000.0}
IMPLIED_CAGR = T.solve_implied_cagr(PRICE, **HLVI, **ASSUMPTIONS)[0]
BASE_VALUE = T.dcf_value_per_share(revenue_cagr=0.12, **HLVI, **ASSUMPTIONS)


class FakeMarket(FakeSEC):
    """FakeSEC plus SEC's ticker map, Halvorsen's FY2024 10-K and a Yahoo chart quote."""

    PAGES = {T.COMPANY_TICKERS_URL: ("application/json", TICKERS_JSON),
             HLVI_10K: ("text/html; charset=utf-8", HLVI_10K_HTML),
             QUOTE_URL: ("application/json", QUOTE_JSON)}

    def __call__(self, method, url, headers, body, timeout):
        if url not in self.PAGES:
            return super().__call__(method, url, headers, body, timeout)
        self.calls.append((url, dict(headers)))
        ctype, text = self.PAGES[url]
        return 200, {"Content-Type": ctype}, text.encode()


def _pitch_brief() -> Brief:
    """What a client without uploads gives: tickers, a question and an SEC contact."""
    return Brief(engagement_id="eng-fr-pitch", specialist="financial-research",
                 objective="A stock pitch with a variant view on Halvorsen Instruments.",
                 intake={"companies": ["HLVI", "BWAC", "CDHS"],
                         "research_question": "What revenue growth does Halvorsen's price imply, "
                                              "and do its filings support more or less?",
                         "pitch_focus": "HLVI; the direction should follow from the analysis",
                         "sec_user_agent": UA})


def variant_view(**changes) -> dict:
    view = {"ticker": "HLVI", "metric": "revenue_cagr", "horizon_years": 10,
            "market_implied": round(IMPLIED_CAGR, 4), "our_view": 0.12,
            "delta": round(0.12 - IMPLIED_CAGR, 4), "direction": "long",
            "thesis": "The price pays for growth below Halvorsen's own record and backlog.",
            "evidence": [{"point": "FY2024 revenue grew 11.8% on analyzer shipments", "claims": ["C1"]},
                         "Backlog rose 24% to $612.0 million, ahead of revenue [C2]",
                         {"point": "Management expects low double-digit growth in 2025", "claims": ["C3"]}],
            "falsifiers": ["Backlog below $500 million at any 2025 quarter-end",
                           "FY2025 revenue growth under 8% with stable pricing"],
            "catalysts": [{"event": "Q3 2025 results and backlog", "date": "2025-11-04"},
                          {"event": "FY2025 10-K with the 2026 outlook", "date": "2026-02"}]}
    return view | changes


def pitch_text(spec, direction: str = "long") -> str:
    implied = f"{IMPLIED_CAGR * 100:.1f}%"
    return f"""# Halvorsen Instruments (HLVI): the backlog outruns what the price pays for

{_disclaimer(spec)}

Direction: {direction}

## Summary
At $41.37 [C4] the price implies revenue growth of {implied} a year for ten years. The FY2024
10-K shows 11.8% growth [C1], a backlog up 24% [C2] and an outlook for low double-digit growth
[C3]. Our base case of 12% a year is worth $50.02 per share, 20.9% above the price
(valuation.csv). The key catalyst is the Q3 2025 backlog; the main risk is a slowdown in
analyzer orders.

## Thesis
1. Growth is running above what is priced in: revenue rose 11.8% in FY2024 [C1].
2. Backlog of $612.0 million, up 24% [C2], covers more than half a year of revenue.

## What the market is pricing in
Halvorsen last traded at $41.37 (Yahoo Finance, delayed, 2025-09-30) [C4]. With FY2024 revenue,
diluted shares and net debt from the 10-K (accession {TEN_K["9900001"][0]}), a 17.0% operating
margin, 21% tax, 35% reinvestment, a 9.0% discount rate and 2.5% terminal growth, the price
implies revenue growth of {implied} a year for ten years (market_implied.json).

## Variant view
We expect 12.0% a year: management guides to low double-digit growth [C3] and the backlog grew
twice as fast as revenue [C2].

## Catalysts
- 2025-11-04: Q3 2025 results and backlog.
- February 2026: the FY2025 10-K and the 2026 outlook.

## Valuation
Bear, base and bull values come from valuation.csv; they are outputs of the stated assumptions,
not a price target.

## Risks and what would change our mind
- Backlog below $500 million at any 2025 quarter-end.
- FY2025 revenue growth under 8% with stable pricing.

## Sources
- Halvorsen FY2024 Form 10-K, accession {TEN_K["9900001"][0]} [C1] [C2] [C3]
- Yahoo Finance chart quote, 2025-09-30 [C4]
"""


def m4_plan(spec, *, view: dict | None = None, pitch: str | None = None,
            after_valuation: list | None = None) -> list:
    return [
        ("sec_company_lookup", {"query": "HLVI"}),
        [("edgar_submissions", {"cik": "9900001", "forms": ["10-K", "10-Q"]}),
         ("edgar_companyfacts", {"cik": "9900001"})],
        ("xbrl_facts", {"cik": "9900001", "metrics": ["revenue", "operating_income"],
                        "fiscal_years": [2022, 2023, 2024]}),
        ("edgar_filing_text", {"url": HLVI_10K}),                              # S1
        ("market_quote", {"ticker": "HLVI"}),                                   # S2
        [*(("record_claim", {"text": quote, "source": "S1", "quote": quote}) for quote in EVIDENCE),
         ("record_claim", {"text": "Halvorsen's delayed last price", "source": "S2",
                           "quote": f'"regularMarketPrice":{PRICE}'})],
        ("reverse_dcf", {"cik": "9900001", **ASSUMPTIONS}),
        ("scenario_valuation", {"scenarios": SCENARIOS}),
        *(after_valuation or []),
        ("write_file", {"path": VIEW, "content": json.dumps(view or variant_view(), indent=2)}),
        ("write_file", {"path": PITCH_MD, "content": pitch or pitch_text(spec)}),
        ("post_progress", {"message": "Stock pitch drafted: long, base case 20.9% above the price."}),
        ("submit_milestone", {"summary": "Stock pitch with a variant view on Halvorsen Instruments.",
                              "artifacts": [PITCH_MD, SNAPSHOT, IMPLIED, VALUATION, VIEW]}),
    ]


def test_m4_stock_pitch_runs_from_tickers_alone(spec, tmp_path):
    ws, transport = tmp_path / "ws", FakeMarket()
    sub, events = _run(spec, ws, m4_plan(spec), M4, brief=_pitch_brief(), transport=transport)
    _assert_ready(spec, ws, sub, events, [PITCH_MD, SNAPSHOT, IMPLIED, VALUATION, VIEW])
    results = _results(sub)
    for name in ("pitch_valuation_recompute", "variant_view_grounded", "ledger_verified",
                 "citations_resolve", "disclaimer_present", "markdown_sections", "no_placeholders"):
        assert results[name].passed is True, (name, results[name].details)
    assert "no_recommendation_language" not in results
    assert results["human_signoff"].kind == "human" and results["human_signoff"].passed is None
    assert sub.milestone_idx == 3 and not (ws / "inputs").exists()     # nothing was uploaded
    # SEC saw the client's contact; the quote host saw a browser-like agent instead
    assert [url for url, _ in transport.calls][-1] == QUOTE_URL and len(events.of_type("egress")) == 5
    assert all(h["User-Agent"] == UA for url, h in transport.calls if "sec.gov" in url)
    assert transport.calls[-1][1]["User-Agent"].startswith("Mozilla/5.0")
    snapshot = json.loads((ws / SNAPSHOT).read_text(encoding="utf-8"))
    assert (snapshot["source"], snapshot["price"], snapshot["source_id"]) == ("yahoo", PRICE, "S2")
    implied = json.loads((ws / IMPLIED).read_text(encoding="utf-8"))
    assert implied["implied_revenue_cagr"] == IMPLIED_CAGR and implied["fiscal_year"] == 2024
    base = next(r for r in csv.DictReader(io.StringIO((ws / VALUATION).read_text(encoding="utf-8")))
                if r["scenario"] == "base")
    assert base["value_per_share"] == f"{BASE_VALUE:.2f}" and float(base["upside_pct"]) > 10
    ledger = json.loads((ws / ".agentkit/ledger.json").read_text(encoding="utf-8"))
    assert [(s["uri"], s["kind"]) for s in ledger["sources"]] == [(HLVI_10K, "tool"), (QUOTE_URL, "tool")]
    assert {PITCH_MD, SNAPSHOT, IMPLIED, VALUATION, VIEW} <= set(ledger["authored"])
    # the rules the model is held to are in its prompt, with the disclaimer for pitch.md
    system = spec.system_prompt(_pitch_brief(), spec.manifest.milestone(M4))
    assert "# Stock pitch with a variant view (M4)" in system
    assert f"verbatim in {PITCH_MD}:\n{_disclaimer(spec)}" in system


@pytest.mark.parametrize("variant,check", [
    ("no_variant_view", "variant_view_grounded"),         # the view is what the price already says
    ("typed_over_value", "pitch_valuation_recompute"),    # a base case edited after the tool ran
    ("uncited_evidence", "variant_view_grounded"),
])
def test_m4_forged_pitch_needs_revision(spec, tmp_path, variant, check):
    view, after = None, None
    if variant == "no_variant_view":
        view = variant_view(our_view=round(IMPLIED_CAGR, 4), delta=0.0)
    elif variant == "typed_over_value":
        after = [("edit_file", {"path": VALUATION, "old_text": f",{BASE_VALUE:.2f},",
                                "new_text": f",{BASE_VALUE + 15:.2f},"})]
    elif variant == "uncited_evidence":
        view = variant_view(evidence=variant_view()["evidence"][:2] + ["Pricing power is intact [C9]"])
    sub, events = _run(spec, tmp_path / "ws", m4_plan(spec, view=view, after_valuation=after), M4,
                       brief=_pitch_brief(), transport=FakeMarket())
    assert not [r for r in _tool_results(events) if r["is_error"]]
    failed = [r for r in sub.check_results if r.kind == "automated" and r.passed is not True]
    assert [r.check for r in failed] == [check], [(r.check, r.details) for r in failed]
    assert sub.status == "needs_revision" and sub.evidence_hash.startswith("0x")


# --- CLI and evals -------------------------------------------------------------------

def _cli(*args):
    out = io.StringIO()
    return main(list(args), stdout=out, env={}), out.getvalue()


def test_cli(spec, tmp_path):
    code, text = _cli("list")
    assert code == 0 and "financial-research" in text
    code, text = _cli("show", "financial-research")
    shown = json.loads(text)
    assert code == 0 and shown["human_review"]["required"] is False and "compute_comps" in shown["tools"]
    code, text = _cli("milestones", "financial-research")
    assert code == 0 and [m["id"] for m in json.loads(text)] == [M1, M2, M3, M4]
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(_brief().intake), encoding="utf-8")
    code, text = _cli("estimate", "financial-research", "--intake", str(intake))
    est = json.loads(text)
    assert code == 0 and (est["hours_low"], est["hours_high"]) == (7, 15)
    assert (est["cost_usd_low"], est["cost_usd_high"]) == (56.0, 120.0)
    # each milestone's high estimate fits within one run's limits
    limits = spec.manifest.limits
    for m in spec.manifest.milestones:
        assert m.hours[1] * spec.manifest.estimate.usd_per_hour <= limits.max_usd
        assert m.hours[1] * 60 <= limits.max_wall_minutes
    assert _cli("validate", "financial-research") == (0, "[]\n")
    assert _cli("validate-intake", "financial-research", "--intake", str(intake))[0] == 0
    intake.write_text(json.dumps({"companies": ["9900003"]}), encoding="utf-8")
    code, text = _cli("validate-intake", "financial-research", "--intake", str(intake))
    assert code == 1 and {m["field"] for m in json.loads(text)} >= {"research_question", "sec_user_agent"}


def test_m2_eval_case_runs_offline_on_its_fixture(spec, tmp_path):
    case = next(c for c in load_cases(spec) if c.name == "m2-comps-fy2024")
    result = run_case(spec, case, adapter=ScriptedAdapter.from_tool_plan(m2_plan()),
                      workspace=tmp_path / "case")
    assert result["status"] == "ready_for_review", result["checks"]
    assert (tmp_path / "case/inputs/edgar/companyfacts/CIK0009900003.json").is_file()
