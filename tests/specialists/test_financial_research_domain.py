"""Domain pack tests for the financial-research specialist: tools, checks,
manifest. Offline: EDGAR JSON comes from synthetic fixtures (fictional
companies) and network calls go through a fake fetch."""
from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentkit.errors import ToolError
from specialists.financial_research import checks as C
from specialists.financial_research import tools as T

PKG = Path(__file__).resolve().parents[2] / "specialists" / "financial_research"
FIXTURE = PKG / "evals" / "fixtures" / "workspace"
CIKS = [9900001, 9900002, 9900003]
UA = "Fixture Research " + "ops" + "@" + "example.com"


@dataclass
class FakeResult:
    url: str
    status: int = 200
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    text: str = ""


class FakeFetch:
    """Serves the fixture JSON for data.sec.gov URLs and records every call."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, *, method="GET", headers=None, body=None):
        self.calls.append((url, dict(headers or {})))
        if url not in self.pages:
            return FakeResult(url, status=404, text="not found")
        return FakeResult(url, text=self.pages[url], body=self.pages[url].encode())


@pytest.fixture()
def ws(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "ws")
    return tmp_path / "ws"


@pytest.fixture()
def built(ws):
    """Workspace with spreads and comps built by the tools."""
    T.build_spreads(ws, ciks=CIKS, fiscal_years=[2023, 2024])
    T.compute_comps(ws, fiscal_year=2024)
    return ws


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


# --- tools ---------------------------------------------------------------------

def test_normalize_cik():
    assert T.normalize_cik(320193) == "0000320193"
    assert T.normalize_cik("CIK0000320193") == "0000320193"
    with pytest.raises(ToolError):
        T.normalize_cik("abc")


def test_edgar_submissions_uses_offline_snapshot_and_filters(ws):
    out = T.edgar_submissions(ws, cik="9900001", forms=["10-K"])
    assert out["origin"] == "cache" and out["name"] == "Halvorsen Instruments Inc."
    assert [f["form"] for f in out["filings"]] == ["10-K"] * 3
    first = out["filings"][0]
    assert first["url"].startswith("https://www.sec.gov/Archives/edgar/data/9900001/")
    assert first["accession"].replace("-", "") in first["url"]


def test_edgar_companyfacts_fetches_with_user_agent_and_caches(tmp_path):
    src = json.loads((FIXTURE / "inputs/edgar/companyfacts/CIK0009900002.json").read_text("utf-8"))
    url = T.COMPANYFACTS_URL.format(cik="0009900002")
    fetch = FakeFetch({url: json.dumps(src)})
    out = T.edgar_companyfacts(tmp_path, fetch=fetch, cik=9900002, user_agent=UA)
    assert out["origin"] == url
    assert out["standard_metrics"]["revenue"] == "Revenues"
    assert fetch.calls[0][1]["User-Agent"] == UA
    assert (tmp_path / ".agentkit/edgar/companyfacts/CIK0009900002.json").is_file()
    # second call is served from the cache
    T.edgar_companyfacts(tmp_path, fetch=fetch, cik=9900002, user_agent=UA)
    assert len(fetch.calls) == 1


def test_edgar_fetch_requires_contact_user_agent_and_fetch(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ToolError, match="User-Agent"):
        T.edgar_submissions(tmp_path, fetch=FakeFetch({}), cik=1, user_agent="no email")
    with pytest.raises(ToolError, match="not available"):
        T.edgar_submissions(tmp_path, fetch=None, cik=1, user_agent=UA)
    with pytest.raises(ToolError, match="not found"):
        T.edgar_submissions(tmp_path, fetch=FakeFetch({}), cik=1, user_agent=UA)


def test_edgar_filing_text_strips_html_pages_and_restricts_host(tmp_path):
    url = "https://www.sec.gov/Archives/edgar/data/9900001/000990000125000027/hlvi-20241231.htm"
    page = ("<html><style>p{}</style><p>Item 7. Results</p><p>Revenue grew 11.8%.</p>"
            + "<p>" + "x" * 3000 + "</p></html>")
    fetch = FakeFetch({url: page})
    out = T.edgar_filing_text(tmp_path, fetch=fetch, url=url, user_agent=UA, max_chars=1000)
    assert out["text"].startswith("Item 7. Results") and "<p>" not in out["text"]
    assert out["next_offset"] == 1000
    again = T.edgar_filing_text(tmp_path, fetch=fetch, url=url, offset=1000)
    assert len(fetch.calls) == 1 and again["offset"] == 1000
    with pytest.raises(ToolError):
        T.edgar_filing_text(tmp_path, fetch=fetch, url="https://evil.example/x.htm")


def test_xbrl_facts_selects_annual_fact_from_own_10k(ws):
    out = T.xbrl_facts(ws, cik=9900003, metrics=["revenue", "cash"], fiscal_years=[2022])
    rev = next(f for f in out["facts"] if f["metric"] == "revenue")
    # the FY2023 10-K restated FY2022 revenue; the original 10-K figure wins
    assert rev["value"] == 690_400_000 and rev["accession"].endswith("-23-000011")
    cash = next(f for f in out["facts"] if f["metric"] == "cash")
    assert cash["period_end"] == "2022-12-31" and cash["period_start"] == ""
    raw = T.xbrl_facts(ws, cik=9900003, tag="Revenues", fiscal_years=[2022])
    assert sorted(r["val"] for r in raw["facts"]) == [684_900_000, 690_400_000]
    with pytest.raises(ToolError):
        T.xbrl_facts(ws, cik=9900003, metrics=["ebitda"], fiscal_years=[2022])


def test_build_spreads_writes_provenance_and_reports_missing(ws):
    out = T.build_spreads(ws, ciks=CIKS, fiscal_years=[2024], metrics=["revenue", "total_equity",
                                                                      "eps_diluted"])
    assert out["missing"] == []
    rows = _rows(ws / T.SPREADS_PATH)
    assert len(rows) == 9 and set(rows[0]) == set(T.SPREADS_COLUMNS)
    hlvi = next(r for r in rows if r["cik"] == "9900001" and r["metric"] == "revenue")
    assert hlvi["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert hlvi["value"] == "1121900000" and hlvi["accession"] == "0009900001-25-000027"
    out = T.build_spreads(ws, ciks=[9900001], fiscal_years=[2019], metrics=["revenue"])
    assert out["rows"] == 0 and out["missing"][0]["fiscal_year"] == 2019
    with pytest.raises(ToolError, match="escapes"):
        T.build_spreads(ws, ciks=[9900001], fiscal_years=[2024], output="../out.csv")


def test_compute_comps_bridge_and_multiples(built):
    rows = {r["cik"]: r for r in _rows(built / T.COMPS_PATH)}
    c = rows["9900003"]
    assert c["total_debt"] == "543000000"
    assert c["market_cap"] == "938910000"          # 17.85 x 52,600,000
    assert c["enterprise_value"] == "1442710000"   # + debt - cash + 12m minority
    assert c["ev_revenue"] == "2.0543" and c["revenue_growth"] == "0.0454"
    h = rows["9900001"]
    assert h["ebitda"] == "238964700" and h["gross_margin"] == "0.54"


def test_compute_comps_needs_inputs_and_flags_gaps(ws):
    with pytest.raises(ToolError, match="build_spreads"):
        T.compute_comps(ws, fiscal_year=2024)
    T.build_spreads(ws, ciks=CIKS, fiscal_years=[2024])
    (ws / T.MARKET_DATA_PATH).write_text("cik,ticker,price,price_as_of\n9900001,HLVI,48.25,"
                                         "2025-06-30\n", encoding="utf-8")
    out = T.compute_comps(ws, fiscal_year=2024)
    assert out["rows"] == 1 and len(out["gaps"]) == 2
    row = _rows(ws / T.COMPS_PATH)[0]
    assert row["revenue_growth"] == ""   # no FY2023 revenue in spreads


def test_filing_red_flags(ws):
    out = T.filing_red_flags(ws, cik=9900003)
    flags = out["flags"]
    assert flags["RF01"]["status"] == "found" and flags["RF02"]["status"] == "found"
    assert flags["RF03"]["filings"][0]["form"] == "10-K/A"
    assert flags["RF04"]["filings"][0]["form"] == "NT 10-K"
    assert flags["RF09"]["status"] == "found"
    clean = T.filing_red_flags(ws, cik=9900001)["flags"]
    assert all(v["status"] == "not_found" for v in clean.values())
    late = T.filing_red_flags(ws, cik=9900003, since="2025-01-01")["flags"]
    assert late["RF01"]["status"] == "not_found" and late["RF09"]["status"] == "found"


def test_index_dataroom(ws):
    out = T.index_dataroom(ws)
    assert out["files"] == 4 and out["unreadable"] == ["inputs/dataroom/board_deck_q4_2024.pdf"]
    rows = {r["path"]: r for r in _rows(ws / T.DATAROOM_INDEX_PATH)}
    pdf = rows["inputs/dataroom/board_deck_q4_2024.pdf"]
    assert pdf["pages"] == "2" and pdf["readable"] == "no" and pdf["note"]
    md = rows["inputs/dataroom/legal/credit_agreement_summary.md"]
    assert md["sha256"] == T.sha256_path(ws / md["path"])


def test_index_dataroom_without_data_room_writes_empty_index(tmp_path):
    out = T.index_dataroom(tmp_path)
    assert out["files"] == 0 and out["root_exists"] is False
    assert (tmp_path / T.DATAROOM_INDEX_PATH).read_text(encoding="utf-8").startswith("path,")
    assert C.dataroom_index_complete(tmp_path, {})["passed"] is True


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in T.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in T.TOOL_DEFS:
        assert set(d) == {"name", "description", "input_schema", "risk", "function"}
        assert d["risk"] in {"read", "write", "exec", "network", "external"}
        assert d["input_schema"]["type"] == "object" and callable(d["function"])


# --- checks: M2 spreads and comps -------------------------------------------------

def test_xbrl_tieout_passes_on_tool_output(built):
    res = C.xbrl_tieout(built, {})
    assert res["passed"] is True and res["score"] == 1.0, res["details"]


@pytest.mark.parametrize("field,value", [
    ("value", "1121900001"),
    ("accession", "0009900001-24-000019"),
    ("period_end", "2024-09-30"),
    ("tag", "GrossProfit"),
])
def test_xbrl_tieout_rejects_forged_rows(built, field, value):
    path = built / C.SPREADS_PATH
    rows = _rows(path)
    target = next(r for r in rows if r["cik"] == "9900001" and r["metric"] == "revenue"
                  and r["fiscal_year"] == "2024")
    target[field] = value
    if field == "tag":
        target["unit"] = "shares"
    _write_rows(path, rows)
    res = C.xbrl_tieout(built, {})
    assert res["passed"] is False and res["score"] < 1.0
    assert "1 unexplained" in res["details"]


def test_xbrl_tieout_accepts_explained_mismatch_within_ratio(built):
    path = built / C.SPREADS_PATH
    rows = _rows(path)
    rows[0]["value"] = str(int(rows[0]["value"]) + 1000)
    rows[0]["note"] = "Non-GAAP adjustment agreed with client"
    _write_rows(path, rows)
    assert C.xbrl_tieout(built, {"min_match_ratio": 0.98})["passed"] is True
    assert C.xbrl_tieout(built, {})["passed"] is False


def test_xbrl_tieout_rejects_real_fact_under_wrong_metric(built):
    path = built / C.SPREADS_PATH
    rows = _rows(path)
    target = next(r for r in rows if r["metric"] == "revenue" and r["cik"] == "9900001")
    target["metric"] = "net_income"   # a real fact, relabelled as another line item
    _write_rows(path, rows)
    res = C.xbrl_tieout(built, {})
    assert res["passed"] is False and "not a standard tag" in res["details"]


def test_comps_tie_to_xbrl_passes_and_catches_forgery(built):
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is True, res["details"]
    comps = built / C.COMPS_PATH
    rows = _rows(comps)
    rows[0]["revenue"] = str(int(rows[0]["revenue"]) + 5_000_000)
    _write_rows(comps, rows)
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is False and "revenue" in res["details"]


def test_comps_tie_to_xbrl_does_not_trust_facts_csv(built):
    """Editing comps.csv and facts.csv together still fails against SEC data."""
    comps, spreads = built / C.COMPS_PATH, built / C.SPREADS_PATH
    crow, srow = _rows(comps), _rows(spreads)
    crow[1]["net_income"] = "99999999"
    for r in srow:
        if r["cik"] == crow[1]["cik"] and r["metric"] == "net_income" and r["fiscal_year"] == "2024":
            r["value"] = "99999999"
    _write_rows(comps, crow)
    _write_rows(spreads, srow)
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is False and "!= reported" in res["details"]


def test_comps_tie_to_xbrl_checks_market_data_and_required(built):
    comps = built / C.COMPS_PATH
    rows = _rows(comps)
    rows[2]["price"] = "19.85"
    rows[0]["operating_income"] = ""
    _write_rows(comps, rows)
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is False
    assert "price" in res["details"] and "blank but required" in res["details"]


def test_comps_recompute(built):
    assert C.comps_recompute(built, {})["passed"] is True
    comps = built / C.COMPS_PATH
    rows = _rows(comps)
    rows[0]["ev_ebitda"] = "9.5"          # hardcoded output
    rows[1]["pe"] = ""                    # dropped output
    _write_rows(comps, rows)
    res = C.comps_recompute(built, {})
    assert res["passed"] is False and "ev_ebitda" in res["details"] and "pe" in res["details"]


def test_comps_checks_fail_when_missing(ws):
    for fn in (C.xbrl_tieout, C.comps_tie_to_xbrl, C.comps_recompute):
        assert fn(ws, {})["passed"] is False


# --- checks: M3 memo ----------------------------------------------------------------

MEMO = """# Coldharbor Systems diligence memo

Not investment advice. This memo is analysis for professional users and does not
constitute a recommendation to buy or sell any security.

## Summary

Coldharbor reported FY2024 revenue of $702.3 million [F:9900003:2024:revenue], up
4.5% [F:9900003:2024:revenue_growth] year over year. Its EBITDA margin of 14.1% [F:9900003:2024:ebitda_margin] trails Halvorsen at 21.3% [F:9900001:2024:ebitda_margin].

| Company | EV/Revenue | EV/EBITDA |
|---|---|---|
| Coldharbor | 2.05x [F:9900003:2024:ev_revenue] | 14.6x [F:9900003:2024:ev_ebitda] |

The company changed auditors in June 2024 and has an undrawn $150 million revolver [C1]. Enterprise value is $1.44bn [F:9900003:2024:enterprise_value].
"""


def _memo(ws: Path, text: str = MEMO) -> None:
    path = ws / C.MEMO_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_memo_figures_match_passes(built):
    _memo(built)
    res = C.memo_figures_match(built, {})
    assert res["passed"] is True, res["details"]


@pytest.mark.parametrize("old,new,reason", [
    ("$702.3 million", "$712.3 million", "shows"),
    ("14.1%", "15.1%", "shows"),
    ("2.05x", "2.15x", "shows"),
    ("up\n4.5% [F:9900003:2024:revenue_growth]", "up\n4.5%", "uncited"),
    (" [C1]", "", "uncited"),
    ("[F:9900003:2024:enterprise_value]", "[F:9900003:2021:enterprise_value]", "no comps row"),
    ("4.5% [F:9900003:2024:revenue_growth]", "4.5x [F:9900003:2024:revenue_growth]",
     "percentage"),
])
def test_memo_figures_match_rejects_mismatch(built, old, new, reason):
    assert old in MEMO
    _memo(built, MEMO.replace(old, new))
    res = C.memo_figures_match(built, {})
    assert res["passed"] is False and reason in res["details"]


def test_no_recommendation_language(built):
    _memo(built)
    assert C.no_recommendation_language(built, {})["passed"] is True
    for bad in ("We rate the shares a Buy.", "Our 12-month price target is $25.",
                "We recommend buying on weakness.", "We initiate coverage with Outperform.",
                "At 2.1x revenue the stock looks a compelling buy."):
        _memo(built, MEMO + "\n" + bad + "\n")
        res = C.no_recommendation_language(built, {})
        assert res["passed"] is False, bad
    assert C.no_recommendation_language(built, {"paths": ["nope.md"]})["passed"] is False


def _checklist(ws: Path, overrides: dict | None = None, drop: str | None = None) -> None:
    hits = {k: v["filings"] for k, v in T.filing_red_flags(ws, cik=9900003)["flags"].items()}
    out = []
    for cik, name in ((9900001, "Halvorsen Instruments Inc."),
                      (9900002, "Brightwater Analytics Corp."),
                      (9900003, "Coldharbor Systems, Inc.")):
        out += [f"## {name} (CIK {cik})", "", "| ID | Item | Status | Evidence |",
                "|---|---|---|---|"]
        for item, title in C.RED_FLAG_ITEMS.items():
            if drop == f"{cik}:{item}":
                continue
            status, evidence = "not_found", "Filing index and 10-K text reviewed"
            if cik == 9900003 and hits.get(item):
                status = "found"
                evidence = ", ".join(f["accession"] for f in hits[item])
            if cik == 9900001 and item == "RF07":
                status, evidence = "found", "inputs/dataroom/legal/related_party_memo.txt [C2]"
            status, evidence = (overrides or {}).get(f"{cik}:{item}", (status, evidence))
            out.append(f"| {item} | {title} | {status} | {evidence} |")
        out.append("")
    path = ws / C.RED_FLAGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


def test_red_flag_checklist_passes(built):
    _checklist(built)
    res = C.red_flag_checklist(built, {})
    assert res["passed"] is True, res["details"]


@pytest.mark.parametrize("overrides,drop,reason", [
    ({"9900003:RF01": ("not_found", "reviewed")}, None, "filing index shows"),
    ({"9900003:RF05": ("found", "see memo")}, None, "found without evidence"),
    ({"9900003:RF02": ("found", "0009900003-24-000011")}, None, "cites none"),
    ({"9900002:RF05": ("found", "0009900002-24-999999")}, None, "not in filing index"),
    ({"9900002:RF06": ("open", "pending")}, None, "status 'open'"),
    (None, "9900001:RF10", "RF10: missing"),
])
def test_red_flag_checklist_rejects_gaps(built, overrides, drop, reason):
    _checklist(built, overrides, drop)
    res = C.red_flag_checklist(built, {})
    assert res["passed"] is False and reason in res["details"]


def test_red_flag_checklist_allow_open_and_missing_section(built):
    _checklist(built, {"9900002:RF06": ("open", "awaiting 10-K text")})
    assert C.red_flag_checklist(built, {"allow_open": True})["passed"] is True
    res = C.red_flag_checklist(built, {"ciks": [9900001, 9900004]})
    assert res["passed"] is False and "no section for CIK 9900004" in res["details"]


# --- checks: M1 sources and data room --------------------------------------------------

def _inventory(ws: Path, mutate=None) -> None:
    filing = T.edgar_submissions(ws, cik=9900002, forms=["10-K"])["filings"][0]
    credit = "inputs/dataroom/legal/credit_agreement_summary.md"
    rows = [
        {"source_id": "S1", "kind": "edgar_filing", "company": "Brightwater Analytics Corp.",
         "cik": "9900002", "form": "10-K", "accession": filing["accession"],
         "filed": filing["filed"], "uri": filing["url"], "sha256": ""},
        {"source_id": "S2", "kind": "edgar_api", "company": "Brightwater Analytics Corp.",
         "cik": "9900002", "form": "", "accession": "", "filed": "",
         "uri": T.COMPANYFACTS_URL.format(cik="0009900002"), "sha256": ""},
        {"source_id": "S3", "kind": "dataroom", "company": "Halvorsen Instruments Inc.",
         "cik": "9900001", "form": "", "accession": "", "filed": "", "uri": credit,
         "sha256": T.sha256_path(ws / credit)},
        {"source_id": "S4", "kind": "web", "company": "", "cik": "", "form": "",
         "accession": "", "filed": "", "uri": "https://example.com/industry-note", "sha256": ""},
    ]
    if mutate:
        mutate(rows)
    path = ws / C.SOURCES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_rows(path, rows)


def test_source_inventory_resolves(ws):
    _inventory(ws)
    res = C.source_inventory_resolves(ws, {})
    assert res["passed"] is True, res["details"]


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r[0].update(accession="0009900002-25-999999"), "not filed by"),
    (lambda r: r[0].update(filed="2020-01-01"), "form/date"),
    (lambda r: r[0].update(uri="https://www.sec.gov/Archives/edgar/data/1/x.htm"), "not under"),
    (lambda r: r[1].update(cik="9900009"), "not a data.sec.gov URL"),
    (lambda r: r[2].update(sha256="0" * 64), "sha256 does not match"),
    (lambda r: r[2].update(uri="../../etc/passwd"), "not a file under inputs/"),
    (lambda r: r[3].update(source_id="S1"), "duplicate"),
    (lambda r: r[3].update(kind="rumor"), "unknown kind"),
])
def test_source_inventory_rejects_unresolvable(ws, mutate, reason):
    _inventory(ws, mutate)
    res = C.source_inventory_resolves(ws, {})
    assert res["passed"] is False and reason in res["details"]


def test_dataroom_index_complete(ws):
    T.index_dataroom(ws)
    assert C.dataroom_index_complete(ws, {})["passed"] is True
    (ws / "inputs/dataroom/finance/late_upload.txt").write_text("new file", encoding="utf-8")
    res = C.dataroom_index_complete(ws, {})
    assert res["passed"] is False and "not indexed" in res["details"] and res["score"] == 0.8


def test_dataroom_index_rejects_tampering(ws):
    T.index_dataroom(ws)
    path = ws / T.DATAROOM_INDEX_PATH
    rows = _rows(path)
    rows[0]["sha256"] = "f" * 64
    pdf = next(r for r in rows if r["path"].endswith(".pdf"))
    pdf["note"] = ""
    rows.append(dict(rows[1], path="inputs/dataroom/ghost.txt"))
    _write_rows(path, rows)
    details = C.dataroom_index_complete(ws, {})["details"]
    assert "sha256 mismatch" in details and "without a note" in details
    assert "indexed but absent" in details


def test_check_defs_signature(built):
    assert set(C.CHECK_DEFS) == {"xbrl_tieout", "comps_tie_to_xbrl", "comps_recompute",
                                 "memo_figures_match", "no_recommendation_language",
                                 "red_flag_checklist", "source_inventory_resolves",
                                 "dataroom_index_complete"}
    for fn in C.CHECK_DEFS.values():
        res = fn(built, {}, run=None)
        assert set(res) == {"passed", "details", "score"}


# --- manifest ----------------------------------------------------------------------

KIT_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "search_files", "run_command",
             "http_fetch", "web_search", "read_document", "record_source", "record_claim",
             "ask_client", "post_progress", "submit_milestone"}
KIT_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
              "json_valid", "csv_columns", "command_succeeds", "ledger_verified",
              "citations_resolve", "disclaimer_present", "rubric_grader", "human_signoff"}


@pytest.fixture(scope="module")
def manifest():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((PKG / "agent.yaml").read_text(encoding="utf-8"))


def test_manifest_parses_and_names_known_tools_and_checks(manifest):
    assert manifest["schema_version"] == 1 and manifest["slug"] == "financial-research"
    domain_tools = {d["name"] for d in T.TOOL_DEFS}
    assert set(manifest["tools"]) <= KIT_TOOLS | domain_tools
    assert domain_tools <= set(manifest["tools"])
    for m in manifest["milestones"]:
        for crit in m["acceptance"]:
            assert crit["check"] in KIT_CHECKS | set(C.CHECK_DEFS), crit["check"]
            if crit["check"] == "rubric_grader":
                rubric = PKG / crit["params"]["rubric"]
                assert rubric.is_file()
        for path in m["deliverables"]:
            assert path.startswith(f"deliverables/{m['id']}/")


def test_manifest_prompt_files_exist_and_keep_boundaries(manifest):
    prompts = [manifest["prompts"]["system"], *manifest["prompts"]["include"]]
    for rel in prompts:
        assert (PKG / rel).is_file(), rel
    system = (PKG / manifest["prompts"]["system"]).read_text(encoding="utf-8")
    assert "Not investment advice" in system and "ask_client" in system
    for d in ("inputs/", "deliverables/"):
        assert d in system
    # the red-flag playbook lists exactly the ids the check enforces
    playbook = (PKG / "playbook/red-flags.md").read_text(encoding="utf-8")
    assert all(f"| {k} |" in playbook for k in C.RED_FLAG_ITEMS)


def test_manifest_policy_and_listing(manifest):
    assert manifest["egress"] == {"mode": "allowlist", "allow": ["data.sec.gov", "www.sec.gov"]}
    assert manifest["shell"]["allow"] == []
    assert manifest["human_gate"]["required"] is False
    assert "Not investment advice" in manifest["human_gate"]["disclaimer"]
    assert manifest["listing"]["pricing"]["currency"] == "USDC"
    assert manifest["models"]["primary"] == "anthropic:claude-opus-5"
    assert [m["id"] for m in manifest["milestones"]] == ["m1-plan-sources", "m2-spreads-comps",
                                                         "m3-diligence-memo"]
    required = {i["field"] for i in manifest["intake"] if i["required"]}
    assert {"companies", "research_question", "sec_user_agent"} <= required


def test_rubrics_are_well_formed():
    yaml = pytest.importorskip("yaml")
    for path in (PKG / "rubrics").glob("*.yaml"):
        rubric = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(rubric) == {"name", "criteria", "threshold"}, path.name
        assert abs(sum(c["weight"] for c in rubric["criteria"]) - 1.0) < 1e-9, path.name
        assert all(set(c) == {"id", "description", "weight"} for c in rubric["criteria"])
