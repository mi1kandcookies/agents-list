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


def test_edgar_filing_text_registers_the_filing_as_a_ledger_source(tmp_path):
    from agentkit.ledger import Ledger
    url = "https://www.sec.gov/Archives/edgar/data/9900003/000990000325000027/cdhs-20241231.htm"
    fetch = FakeFetch({url: "<html><p>Item 9. The Audit Committee dismissed the prior auditor.</p></html>"})
    ledger = Ledger(tmp_path)
    out = T.edgar_filing_text(tmp_path, fetch=fetch, ledger=ledger, url=url, user_agent=UA)
    assert out["source_id"] == "S1"
    src = ledger.source("S1")
    assert src.uri == url and src.kind == "tool"
    assert ledger.add_claim("Auditor dismissed", "S1", "The Audit Committee dismissed the prior auditor").id == "C1"
    # paging through the cached snapshot keeps the same source
    assert T.edgar_filing_text(tmp_path, ledger=ledger, url=url, offset=10)["source_id"] == "S1"
    assert len(ledger.sources) == 1 and len(fetch.calls) == 1


def test_tool_paths_follow_the_kit_workspace_rules(ws):
    market = (ws / T.MARKET_DATA_PATH).read_bytes()
    with pytest.raises(ToolError, match="read-only"):
        T.build_spreads(ws, ciks=[9900001], fiscal_years=[2024], output=T.MARKET_DATA_PATH)
    with pytest.raises(ToolError, match="internal"):
        T.index_dataroom(ws, output=".agentkit/ledger.json")
    with pytest.raises(ToolError, match="internal"):
        T.compute_comps(ws, fiscal_year=2024, spreads=".agentkit/edgar/facts.csv")
    assert (ws / T.MARKET_DATA_PATH).read_bytes() == market
    # a data-room root at the workspace itself never lists kit internals
    (ws / ".agentkit").mkdir(exist_ok=True)
    (ws / ".agentkit" / "ledger.json").write_text("{}", encoding="utf-8")
    listed = T.index_dataroom(ws, root=".")
    assert listed["files"] > 4
    assert not any(r["path"].startswith(".agentkit/") for r in _rows(ws / T.DATAROOM_INDEX_PATH))


def test_xbrl_facts_selects_the_latest_10k_figure_for_the_period(ws):
    out = T.xbrl_facts(ws, cik=9900003, metrics=["revenue", "cash"], fiscal_years=[2022, 2023])
    rev = {f["fiscal_year"]: f for f in out["facts"] if f["metric"] == "revenue"}
    # the FY2023 10-K restated FY2022 revenue (690.4m -> 684.9m): the restated
    # figure wins, so FY2023 growth compares two figures on the same basis
    assert rev[2022]["value"] == 684_900_000 and rev[2022]["accession"].endswith("-24-000019")
    assert rev[2023]["value"] == 671_800_000 and rev[2023]["accession"].endswith("-25-000027")
    cash = next(f for f in out["facts"] if f["metric"] == "cash")
    assert cash["period_end"] == "2022-12-31" and cash["period_start"] == ""
    raw = T.xbrl_facts(ws, cik=9900003, tag="Revenues", fiscal_years=[2022])
    assert sorted(r["val"] for r in raw["facts"]) == [684_900_000, 690_400_000]
    assert {r["fiscal_year"] for r in raw["facts"]} == {2022}
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


def _companyfacts(cik: int, name: str, facts: list[tuple]) -> dict:
    """Synthetic companyfacts JSON from (tag, unit, start, end, value, accession,
    fy, filed) tuples, all reported on Form 10-K."""
    gaap: dict = {}
    for tag, unit, start, end, val, accn, fy, filed in facts:
        rec = {"accn": accn, "end": end, "filed": filed, "form": "10-K", "fp": "FY", "fy": fy,
               "val": val}
        if start:
            rec["start"] = start
        gaap.setdefault(tag, {"units": {}})["units"].setdefault(unit, []).append(rec)
    return {"cik": cik, "entityName": name, "facts": {"us-gaap": gaap}}


def _save_companyfacts(ws: Path, data: dict) -> None:
    path = ws / "inputs/edgar/companyfacts" / f"CIK{data['cik']:010d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# A 52/53-week filer: fiscal years end on the Saturday nearest 31 December, so
# FY2020 ends 2021-01-02, FY2021 ends 2022-01-01 and FY2022 ends 2022-12-31.
WEEKS = [("2019-12-29", "2021-01-02", 800), ("2021-01-03", "2022-01-01", 1000),
         ("2022-01-02", "2022-12-31", 1100)]
TEN_KS = {2020: ("0009900009-21-000005", "2021-03-01"), 2021: ("0009900009-22-000005", "2022-03-01"),
          2022: ("0009900009-23-000005", "2023-03-01")}


def _weeks_filer() -> dict:
    facts = []
    for fy, (accn, filed) in TEN_KS.items():
        # each 10-K reports its own year and the prior year as a comparative
        for start, end, val in WEEKS[max(0, fy - 2021): fy - 2019]:
            facts.append(("Revenues", "USD", start, end, val * 1_000_000, accn, fy, filed))
    return _companyfacts(9900009, "Weekly Provisions Inc.", facts)


def test_fiscal_years_follow_the_10k_for_52_53_week_filers(tmp_path):
    data = _weeks_filer()
    ends = {2020: "2021-01-02", 2021: "2022-01-01", 2022: "2022-12-31"}
    assert T.fiscal_year_ends(data) == ends
    assert C._fy_ends(data) == ends                        # the check derives the same years
    assert T.annual_fact(data, "revenue", 2021)["value"] == 1_000_000_000
    fy22 = T.annual_fact(data, "revenue", 2022)
    assert fy22["value"] == 1_100_000_000 and fy22["period_end"] == "2022-12-31"
    _save_companyfacts(tmp_path, data)
    T.build_spreads(tmp_path, ciks=[9900009], fiscal_years=[2021, 2022], metrics=["revenue"])
    rows = _rows(tmp_path / T.SPREADS_PATH)
    assert [(r["fiscal_year"], r["value"]) for r in rows] == [("2021", "1000000000"),
                                                              ("2022", "1100000000")]
    assert C.xbrl_tieout(tmp_path, {})["passed"] is True
    # the old calendar-year label (FY2022 for the year ending 2022-01-01) is caught
    rows[0]["fiscal_year"] = "2022"
    rows[1]["fiscal_year"] = "2023"
    _write_rows(tmp_path / T.SPREADS_PATH, rows)
    res = C.xbrl_tieout(tmp_path, {})
    assert res["passed"] is False and "is not the FY2022 year-end 2022-12-31" in res["details"]


def test_fiscal_year_labels_for_january_year_ends_and_bad_fy():
    retail = _companyfacts(9900010, "Retail", [
        ("Revenues", "USD", "2024-02-04", "2025-02-01", 5, "0009900010-25-000001", 2024, "2025-03-20")])
    assert T.fiscal_year_ends(retail) == C._fy_ends(retail) == {2024: "2025-02-01"}
    wrong = _companyfacts(9900011, "Wrong fy", [
        ("Revenues", "USD", "2022-01-01", "2022-12-31", 5, "0009900011-23-000001", 2019, "2023-03-01")])
    assert T.fiscal_year_ends(wrong) == C._fy_ends(wrong) == {2022: "2022-12-31"}


def test_fixture_fiscal_years_agree_between_tool_and_check():
    for cik in CIKS:
        data = json.loads((FIXTURE / f"inputs/edgar/companyfacts/CIK{cik:010d}.json").read_text("utf-8"))
        # FY2021 appears only as a comparative in the FY2022 10-K
        assert T.fiscal_year_ends(data) == C._fy_ends(data) == {
            2021: "2021-12-31", 2022: "2022-12-31", 2023: "2023-12-31", 2024: "2024-12-31"}


def test_debt_total_counts_each_borrowing_once():
    from decimal import Decimal as D
    # LongTermDebt already includes its 45 current portion; 200 of commercial paper sits apart
    assert T.debt_total({"long_term_debt_total": D(543), "long_term_debt_current": D(45),
                         "short_term_borrowings": D(200)}) == (D(743), "long_term_debt_total + "
                                                                       "short_term_borrowings")
    assert T.debt_total({"long_term_debt_noncurrent": D(498), "long_term_debt_current": D(45),
                         "short_term_borrowings": D(200)})[0] == 743
    assert T.debt_total({"long_term_debt_noncurrent": D(498), "debt_current": D(245),
                         "long_term_debt_current": D(45), "short_term_borrowings": D(200)})[0] == 743
    assert T.debt_total({"debt_current": D(245), "long_term_debt_current": D(45)})[0] == 245
    assert T.debt_total({}) == (None, "no debt reported")


def test_comps_debt_from_a_long_term_total_and_commercial_paper(tmp_path):
    accn, fy, filed = "0009900012-25-000001", 2024, "2025-02-20"
    year = ("2024-01-01", "2024-12-31")
    data = _companyfacts(9900012, "Paper Mill Corp.", [
        ("Revenues", "USD", *year, 1_000_000_000, accn, fy, filed),
        ("OperatingIncomeLoss", "USD", *year, 150_000_000, accn, fy, filed),
        ("DepreciationDepletionAndAmortization", "USD", *year, 50_000_000, accn, fy, filed),
        ("NetIncomeLoss", "USD", *year, 90_000_000, accn, fy, filed),
        ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", *year, 10_000_000, accn, fy, filed),
        ("CashAndCashEquivalentsAtCarryingValue", "USD", None, "2024-12-31", 43_000_000, accn, fy, filed),
        ("LongTermDebt", "USD", None, "2024-12-31", 543_000_000, accn, fy, filed),
        ("LongTermDebtCurrent", "USD", None, "2024-12-31", 45_000_000, accn, fy, filed),
        ("ShortTermBorrowings", "USD", None, "2024-12-31", 200_000_000, accn, fy, filed)])
    _save_companyfacts(tmp_path, data)
    (tmp_path / T.MARKET_DATA_PATH).write_text("cik,ticker,price,price_as_of\n9900012,PMIL,20,"
                                               "2025-06-30\n", encoding="utf-8")
    T.build_spreads(tmp_path, ciks=[9900012], fiscal_years=[2024])
    out = T.compute_comps(tmp_path, fiscal_year=2024)
    assert out["debt_basis"]["9900012"].startswith("long_term_debt_total")
    row = _rows(tmp_path / T.COMPS_PATH)[0]
    assert row["total_debt"] == "743000000"            # not 543m + 45m = 588m
    assert row["enterprise_value"] == str(200_000_000 + 743_000_000 - 43_000_000)
    for fn in (C.xbrl_tieout, C.comps_tie_to_xbrl, C.comps_recompute):
        assert fn(tmp_path, {})["passed"] is True, fn(tmp_path, {})["details"]
    # the double count the old tag map produced no longer recomputes
    rows = _rows(tmp_path / T.COMPS_PATH)
    rows[0]["total_debt"] = "588000000"
    _write_rows(tmp_path / T.COMPS_PATH, rows)
    res = C.comps_recompute(tmp_path, {})
    assert res["passed"] is False and "total_debt" in res["details"]


def test_compute_comps_needs_inputs_and_flags_gaps(ws):
    with pytest.raises(ToolError, match="build_spreads"):
        T.compute_comps(ws, fiscal_year=2024)
    T.build_spreads(ws, ciks=CIKS, fiscal_years=[2024])
    (ws / T.MARKET_DATA_PATH).write_text("cik,ticker,price,price_as_of\n9900001,HLVI,48.25,"
                                         "2025-06-30\n", encoding="utf-8")
    out = T.compute_comps(ws, fiscal_year=2024)
    # a company without a market row keeps its operating metrics; only the
    # valuation columns stay blank
    assert out["rows"] == 3 and len(out["gaps"]) == 2 and "no market data row" in out["gaps"][0]
    rows = {r["cik"]: r for r in _rows(ws / T.COMPS_PATH)}
    assert rows["9900001"]["revenue_growth"] == ""   # no FY2023 revenue in spreads
    assert rows["9900001"]["enterprise_value"] and rows["9900001"]["minority_interest"] == "0"
    other = rows["9900002"]
    assert other["price"] == other["market_cap"] == other["enterprise_value"] == other["pe"] == ""
    assert other["minority_interest"] == "" and other["ebitda_margin"] and other["ticker"] == "BWAC"
    assert C.comps_recompute(ws, {})["passed"] is True
    # spreads built without FY2023 leave revenue_prior blank although the SEC reports it
    res = C.comps_tie_to_xbrl(ws, {})
    assert res["passed"] is False
    assert "revenue_prior: blank but the SEC source reports revenue for FY2023" in res["details"]
    assert "price" not in res["details"]      # blank valuation columns without a market row are fine


def test_compute_comps_without_market_data_writes_operating_comps(built):
    (built / T.MARKET_DATA_PATH).unlink()
    out = T.compute_comps(built, fiscal_year=2024)
    assert out["rows"] == 3 and out["market_data"] is False and "not found" in out["gaps"][0]
    for row in _rows(built / T.COMPS_PATH):
        assert row["price"] == row["enterprise_value"] == row["ev_revenue"] == ""
        assert row["revenue"] and row["gross_margin"] and row["revenue_growth"]
    for fn in (C.comps_tie_to_xbrl, C.comps_recompute):
        assert fn(built, {})["passed"] is True, fn(built, {})["details"]
    # a price with no client market data behind it fails
    path = built / C.COMPS_PATH
    rows = _rows(path)
    rows[0]["price"] = "48.25"
    _write_rows(path, rows)
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is False and "without a client market data row" in res["details"]
    res = C.comps_tie_to_xbrl(built, {"require_market_data": True})
    assert res["passed"] is False and "market data is required" in res["details"]


def test_compute_comps_uses_a_current_share_count_and_parses_formatted_numbers(built):
    (built / T.MARKET_DATA_PATH).write_text(
        "cik,ticker,price,price_as_of,shares_outstanding,minority_interest,preferred\n"
        '9900001,HLVI,"1,048.25",2025-06-30,,0,0\n'
        "9900002,BWAC,$61.40,2025-06-30,,0,0\n"
        '9900003,CDHS,17.85,2025-06-30,"52,450,000","12,000,000",0\n', encoding="utf-8")
    T.compute_comps(built, fiscal_year=2024)
    rows = {r["cik"]: r for r in _rows(built / T.COMPS_PATH)}
    assert rows["9900001"]["price"] == "1048.25" and rows["9900002"]["price"] == "61.4"
    c = rows["9900003"]
    assert c["shares_outstanding"] == "52450000"
    assert c["market_cap"] == "936232500"          # 17.85 x 52,450,000 current shares
    for fn in (C.comps_tie_to_xbrl, C.comps_recompute):
        assert fn(built, {})["passed"] is True, fn(built, {})["details"]
    (built / T.MARKET_DATA_PATH).write_text("cik,ticker,price,price_as_of\n9900001,HLVI,n/a,"
                                            "2025-06-30\n", encoding="utf-8")
    with pytest.raises(ToolError, match="not a number"):
        T.compute_comps(built, fiscal_year=2024)


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


def test_index_dataroom_notes_every_file_the_agent_cannot_read(ws):
    room = ws / "inputs/dataroom"
    (room / "cim.pdf").write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Page /Resources << /Font << /F1 2 0 R"
                                   b" >> >> >> endobj\nBT /F1 12 Tf (Confidential) Tj ET\n%%EOF\n")
    (room / "finance/model.xlsx").write_bytes(b"PK\x03\x04 synthetic workbook")
    (room / "mgmt_deck.pptx").write_bytes(b"PK\x03\x04 synthetic deck")
    T.index_dataroom(ws)
    rows = {r["path"]: r for r in _rows(ws / T.DATAROOM_INDEX_PATH)}
    pdf = rows["inputs/dataroom/cim.pdf"]
    assert pdf["readable"] == "unknown" and "no PDF reader" in pdf["note"]
    for rel in ("inputs/dataroom/finance/model.xlsx", "inputs/dataroom/mgmt_deck.pptx"):
        assert rows[rel]["readable"] == "no" and "no reader" in rows[rel]["note"]
    assert rows["inputs/dataroom/legal/credit_agreement_summary.md"]["readable"] == "yes"
    res = C.dataroom_index_complete(ws, {})
    assert res["passed"] is True, res["details"]
    # claiming a spreadsheet is readable fails
    path = ws / T.DATAROOM_INDEX_PATH
    listed = _rows(path)
    next(r for r in listed if r["path"].endswith(".xlsx")).update(readable="yes", note="")
    _write_rows(path, listed)
    res = C.dataroom_index_complete(ws, {})
    assert res["passed"] is False and "marked readable but no reader for .xlsx" in res["details"]


def test_sec_company_lookup_resolves_tickers_and_names(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    tickers = {"0": {"cik_str": 9900003, "ticker": "CDHS", "title": "Coldharbor Systems, Inc."},
               "1": {"cik_str": 9900001, "ticker": "HLVI", "title": "Halvorsen Instruments Inc."},
               "2": {"cik_str": 9900002, "ticker": "BWAC", "title": "Brightwater Analytics Corp."}}
    fetch = FakeFetch({T.COMPANY_TICKERS_URL: json.dumps(tickers)})
    with pytest.raises(ToolError, match="User-Agent"):
        T.sec_company_lookup(tmp_path, fetch=fetch, query="CDHS")
    out = T.sec_company_lookup(tmp_path, fetch=fetch, query="cdhs", user_agent=UA)
    assert out["origin"] == T.COMPANY_TICKERS_URL and fetch.calls[0][1]["User-Agent"] == UA
    assert out["matches"] == [{"cik": "9900003", "ticker": "CDHS", "name": "Coldharbor Systems, Inc."}]
    by_name = T.sec_company_lookup(tmp_path, fetch=fetch, query="analytics")
    assert by_name["origin"] == "cache" and [m["cik"] for m in by_name["matches"]] == ["9900002"]
    assert len(fetch.calls) == 1
    assert T.sec_company_lookup(tmp_path, query="Nonexistent Holdings")["matches"] == []
    assert [m["ticker"] for m in T.sec_company_lookup(tmp_path, query="CIK0009900001")["matches"]] \
        == ["HLVI"]
    with pytest.raises(ToolError):
        T.sec_company_lookup(tmp_path, query=" ")


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


def _recomputed(rows: list[dict]) -> list[dict]:
    """Rows with every derived column rewritten from the row's own inputs."""
    for row in rows:
        for col, value in C._expected_derived(row).items():
            row[col] = "" if value is None else f"{value:.10g}"
    return rows


def test_comps_blanked_cash_and_debt_fail_although_they_recompute(built):
    """Blanking cash and debt inflates EV by ~34% while the row stays
    internally consistent; the SEC source still reports those balances."""
    path = built / C.COMPS_PATH
    rows = _rows(path)
    target = next(r for r in rows if r["cik"] == "9900003")
    for col in ("cash", "long_term_debt_noncurrent", "long_term_debt_current"):
        target[col] = ""
    _write_rows(path, _recomputed(rows))
    assert target["enterprise_value"] == "950910000"
    assert C.comps_recompute(built, {})["passed"] is True
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is False
    assert "9900003 cash: blank but facts.csv has cash FY2024" in res["details"]
    assert "long_term_debt_current: blank" in res["details"]
    # dropping the rows from facts.csv too does not help: the SEC source reports them
    spreads = built / C.SPREADS_PATH
    _write_rows(spreads, [r for r in _rows(spreads) if not (r["cik"] == "9900003" and r["metric"] in (
        "cash", "long_term_debt_noncurrent", "long_term_debt_current"))])
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is False and "blank but the SEC source reports cash for FY2024" in res["details"]


def test_comps_must_cover_every_company_in_scope(built):
    path = built / C.COMPS_PATH
    rows = _rows(path)
    _write_rows(path, [r for r in rows if r["cik"] != "9900002"])
    res = C.comps_tie_to_xbrl(built, {})
    assert res["passed"] is False
    assert "9900002: no FY2024 comps row although the SEC source reports its revenue" in res["details"]
    # the brief's companies count even when the agent dropped one everywhere
    _write_rows(path, rows)
    res = C.comps_tie_to_xbrl(built, {"ciks": CIKS + [9900004]})
    assert res["passed"] is False and "9900004: no FY2024 comps row and no companyfacts" in res["details"]
    assert C.comps_tie_to_xbrl(built, {"ciks": CIKS})["passed"] is True


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

Coldharbor changed auditors in June 2024. Halvorsen has an undrawn $150 million revolver [C1]. Enterprise value is $1.44bn [F:9900003:2024:enterprise_value].
"""
CREDIT = "inputs/dataroom/legal/credit_agreement_summary.md"


def _memo(ws: Path, text: str = MEMO) -> None:
    """Write the memo; the first call also records claim C1 (the revolver)."""
    from agentkit.ledger import Ledger
    ledger = Ledger(ws)
    if not ledger.claims:
        src = ledger.add_source(f"workspace:{CREDIT}", "Credit agreement summary",
                                (ws / CREDIT).read_text(encoding="utf-8"), kind="customer")
        ledger.add_claim("Halvorsen's revolver is undrawn", src.id,
                         "Revolving facility: $150 million, undrawn at 2024-12-31")
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
    # a claim citation only covers figures its verbatim quote contains
    ("$150 million revolver [C1]", "$950 million revolver [C1]", "not in the quote of C1"),
    ("$150 million revolver [C1]", "$150 million revolver, up 38% [C1]", "38% not in the quote"),
    # precision the playbook asks for
    ("$702.3 million [F:", "$1 billion [F:", "too rounded"),
    ("4.5% [F:", "5% [F:", "too rounded"),
    ("$1.44bn", "$1.4bn", "too rounded"),
    # direction words set the sign: revenue grew 4.54%
    ("up\n4.5%", "down\n4.5%", "shows -4.5"),
    ("up\n4.5%", "declined\n4.5%", "shows -4.5"),
])
def test_memo_figures_match_rejects_mismatch(built, old, new, reason):
    assert old in MEMO
    _memo(built, MEMO.replace(old, new))
    res = C.memo_figures_match(built, {})
    assert res["passed"] is False and reason in res["details"], res["details"]


@pytest.mark.parametrize("old,new", [
    ("$702.3 million", "$702 million"),                    # three significant figures
    ("$702.3 million", "$0.7023 billion"),
    ("$150 million revolver [C1]", "$150,000,000 revolver [C1]"),
    ("$150 million revolver [C1]", "$150 million revolver [C1, C2]"),
    ("2.05x", "2.1x"),
])
def test_memo_figures_match_accepts_equivalent_forms(built, old, new):
    _memo(built, MEMO.replace(old, new))
    res = C.memo_figures_match(built, {})
    assert res["passed"] is True, res["details"]


def test_memo_bullet_dash_is_not_a_minus_sign(built):
    _memo(built, MEMO + "\n- 14.1% [F:9900003:2024:ebitda_margin] EBITDA margin\n"
                        "- -4.5% [F:9900003:2024:revenue_growth] would be a decline\n")
    res = C.memo_figures_match(built, {})
    assert res["passed"] is False and res["details"].count("shows") == 1
    assert "shows -4.5%" in res["details"]


def test_memo_bullets_are_checked_one_by_one(built):
    _memo(built, MEMO + "\n- Backlog fell to $120 million\n- Customer churn hit 18%\n"
                        "- The revolver matures in 2028 [C1]\n")
    res = C.memo_figures_match(built, {})
    assert res["passed"] is False and res["details"].count("uncited figure") == 2


@pytest.mark.parametrize("sentence", [
    "Backlog grew 25 percent to 900M.", "The deal values it at 3.1 times revenue.",
    "Backlog was 902,300,000 at year end.", "Backlog is now 1.2B.", "Leverage is 4.5 turns.",
    "Churn improved by 150 bps.", "The company has 12500 customers.",
    "Gross margin widened 1.2pp.", "Margin fell 3 percentage points.",
])
def test_memo_figures_in_other_formats_need_a_citation(built, sentence):
    _memo(built, MEMO + "\n" + sentence + "\n")
    res = C.memo_figures_match(built, {})
    assert res["passed"] is False and "uncited figure" in res["details"], sentence


def test_memo_references_and_years_are_not_figures(built):
    _memo(built, MEMO + "\nSee the FY2024 Form 10-K (accession 0009900003-25-000027, CIK 9900003) at "
                        "https://www.sec.gov/Archives/edgar/data/9900003/000990000325000027/"
                        "cdhs-20241231.htm and inputs/dataroom/board_deck_q4_2024.pdf, filed in "
                        "February 2025 under Item 4.01.\n")
    res = C.memo_figures_match(built, {})
    assert res["passed"] is True, res["details"]


def test_memo_negative_growth_reads_its_direction_words(built):
    path = built / C.COMPS_PATH
    rows = _rows(path)
    next(r for r in rows if r["cik"] == "9900003")["revenue_growth"] = "-0.0454"
    _write_rows(path, rows)
    for word, passed in (("declined", True), ("fell by", True), ("down", True), ("up", False),
                         ("grew", False)):
        _memo(built, MEMO.replace("up\n4.5%", f"{word}\n4.5%"))
        assert C.memo_figures_match(built, {})["passed"] is passed, word
    _memo(built, MEMO.replace("up\n4.5%", "at\n-4.5%"))
    assert C.memo_figures_match(built, {})["passed"] is True


def test_memo_figure_tag_may_wrap_to_next_line(built):
    wrapped = MEMO.replace("of $702.3 million [F:9900003:2024:revenue], up",
                           "of $702.3 million\n[F:9900003:2024:revenue], up")
    assert wrapped != MEMO
    _memo(built, wrapped)
    assert C.memo_figures_match(built, {})["passed"] is True


def test_no_recommendation_language_allows_ordinary_prose(built):
    from specialists.financial_research.agent import FinancialResearch
    disclaimer = FinancialResearch().manifest.human_gate.disclaimer
    _memo(built, MEMO + "\nThe policy rate hold helped rates hold steady; the sell-side "
                        "consensus is not a buy-side view. Its credit rating is Ba2.\n"
                        "Interest rates held at a neutral level. Impact on margins: neutral.\n"
                        "We note the fair value of the warrants; goodwill fair value exceeded "
                        "its carrying amount.\nManagement's revenue target of $800 million "
                        "assumes a 20% downside case for backlog; the downside risk to margins "
                        "is labor.\nThe company should sell its legacy unit, management says.\n"
                        + disclaimer + "\n")
    res = C.no_recommendation_language(built, {})
    assert res["passed"] is True, res["details"]


@pytest.mark.parametrize("bad", [
    "We rate the shares a Buy.", "Our 12-month price target is $25.",
    "We recommend buying on weakness.", "We initiate coverage with Outperform.",
    "At 2.1x revenue the stock looks a compelling buy.",
    "We rate the shares as Overweight.", "Reiterate our Outperform rating.",
    # phrasings the first version of the check let through
    "We rate Coldharbor a Strong Buy.", "We rate Coldharbor a Buy.", "Rating: Buy",
    "Rating: Neutral", "Coldharbor: Strong Buy (12-month).", "Coldharbor: Buy (12-month).",
    "Investors should buy the shares below $30.", "Our fair value estimate is $52 per share.",
    "We see 40% upside to $42.", "We value the shares at $30.",
    "The shares are worth about $52 per share.",
    # a hedge on the same line no longer exempts it
    "We would recommend buying on weakness; this does not constitute investment advice.",
    "Not a recommendation, but the stock is a buy.",
])
def test_no_recommendation_language(built, bad):
    _memo(built)
    assert C.no_recommendation_language(built, {})["passed"] is True
    _memo(built, MEMO + "\n" + bad + "\n")
    res = C.no_recommendation_language(built, {})
    assert res["passed"] is False, bad


def test_no_recommendation_language_needs_its_files(built):
    assert C.no_recommendation_language(built, {"paths": ["nope.md"]})["passed"] is False


def _checklist(ws: Path, overrides: dict | None = None, drop: str | None = None,
               window: str | None = "Window: 2022-11-04 to 2025-11-04") -> None:
    hits = {k: v["filings"] for k, v in T.filing_red_flags(ws, cik=9900003)["flags"].items()}
    out = []
    for cik, name in ((9900001, "Halvorsen Instruments Inc."),
                      (9900002, "Brightwater Analytics Corp."),
                      (9900003, "Coldharbor Systems, Inc.")):
        out += [f"## {name} (CIK {cik})", "", *([window, ""] if window else []),
                "| ID | Item | Status | Evidence |", "|---|---|---|---|"]
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


def test_red_flag_checklist_covers_companies_dropped_from_comps(built):
    """A company removed from comps.csv is still in facts.csv, so it stays in scope."""
    _checklist(built, drop="9900002:RF01")
    comps = built / C.COMPS_PATH
    _write_rows(comps, [r for r in _rows(comps) if r["cik"] != "9900002"])
    res = C.red_flag_checklist(built, {})
    assert res["passed"] is False and "CIK 9900002 RF01: missing" in res["details"]


def test_red_flag_checklist_states_a_window_covering_the_lookback(built):
    _checklist(built)
    assert C.red_flag_checklist(built, {"lookback_years": 3})["passed"] is True
    _checklist(built, window=None)
    res = C.red_flag_checklist(built, {"lookback_years": 3})
    assert res["passed"] is False and "state the review window (Window: 2022-11-04 to 2025-11-04)" \
        in res["details"]
    _checklist(built, window="Window: 2024-01-01 to 2025-11-04")        # one year short
    res = C.red_flag_checklist(built, {"lookback_years": 3})
    assert res["passed"] is False and "starts 2024-01-01, after 2022-11-04" in res["details"]
    _checklist(built, window="Review window 2020-01-01 through 2025-11-04")   # longer is fine
    assert C.red_flag_checklist(built, {"lookback_years": 3})["passed"] is True


def test_red_flag_evidence_files_must_exist(built):
    _checklist(built, {"9900001:RF07": ("found", "inputs/dataroom/legal/lease_side_letter.txt")})
    res = C.red_flag_checklist(built, {})
    assert res["passed"] is False and "no such input file" in res["details"]
    _checklist(built, {"9900001:RF07": ("found", "inputs/../../etc/passwd")})
    assert "no such input file" in C.red_flag_checklist(built, {})["details"]


OLDER_PAGE = "CIK0009900001-submissions-001.json"


def _older_index_page(ws: Path) -> dict:
    """Halvorsen's filing index gains an older page SEC lists but nobody loaded:
    an auditor change in December 2022 (inside a three-year window ending
    2025-11-04) and a non-reliance notice in June 2022 (outside it)."""
    main = json.loads((ws / "inputs/edgar/submissions/CIK0009900001.json").read_text("utf-8"))
    main["filings"]["files"] = [{"name": OLDER_PAGE, "filingCount": 2,
                                 "filingFrom": "2022-06-01", "filingTo": "2022-12-15"}]
    path = ws / ".agentkit/edgar/submissions/CIK0009900001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(main), encoding="utf-8")
    return {"accessionNumber": ["0009900001-22-000041", "0009900001-22-000031"],
            "filingDate": ["2022-12-15", "2022-06-01"], "reportDate": ["2022-12-12", "2022-05-27"],
            "form": ["8-K", "8-K"], "items": ["4.01,9.01", "4.02"],
            "primaryDocument": ["hlvi-8k-auditor.htm", "hlvi-8k-nonreliance.htm"]}


def test_red_flag_window_must_be_covered_by_the_filing_index(built):
    page = _older_index_page(built)
    flags = T.filing_red_flags(built, cik=9900001)
    assert flags["window"] == {"from": "2022-11-04", "to": "2025-11-04"}
    assert flags["complete"] is False and OLDER_PAGE in flags["note"]
    _checklist(built)
    res = C.red_flag_checklist(built, {"lookback_years": 3})
    assert res["passed"] is False and "not loaded back to 2022-11-04" in res["details"]
    # edgar_submissions(since=...) fetches the page; the hit inside the window must be found
    fetch = FakeFetch({T.SUBMISSIONS_PAGE_URL.format(name=OLDER_PAGE): json.dumps(page)})
    out = T.edgar_submissions(built, fetch=fetch, cik=9900001, since="2022-11-04", user_agent=UA)
    assert out["older_pages_not_loaded"] == [] and out["index_covers"]["from"] == "2022-06-01"
    assert fetch.calls[0][1]["User-Agent"] == UA
    flags = T.filing_red_flags(built, cik=9900001)
    assert flags["complete"] is True and flags["flags"]["RF01"]["status"] == "found"
    assert flags["flags"]["RF02"]["status"] == "not_found"          # June 2022 is outside
    res = C.red_flag_checklist(built, {"lookback_years": 3})
    assert res["passed"] is False and "CIK 9900001 RF01: filing index shows 0009900001-22-000041" \
        in res["details"]
    _checklist(built, {"9900001:RF01": ("found", "0009900001-22-000041")})
    assert C.red_flag_checklist(built, {"lookback_years": 3})["passed"] is True


# --- checks: M1 sources and data room --------------------------------------------------

WEB_NOTE = "https://example.com/industry-note"


def _inventory(ws: Path, mutate=None) -> None:
    from agentkit.ledger import Ledger
    Ledger(ws).add_source(WEB_NOTE, "Industry note", "Industry note text.", kind="web")
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
         "accession": "", "filed": "", "uri": WEB_NOTE, "sha256": ""},
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
    (lambda r: r[2].update(uri="inputs/../../outside.txt"), "not a file under inputs/"),
    (lambda r: r[3].update(source_id="S1"), "duplicate"),
    (lambda r: r[3].update(kind="rumor"), "unknown kind"),
    # a web page nobody fetched cannot be listed as a source
    (lambda r: r[3].update(uri="https://example.com/invented-analyst-report"), "never retrieved"),
    (lambda r: r[3].update(uri="ftp://example.com/x"), "without an http(s) uri"),
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


def test_check_param_paths_stay_in_the_workspace(built):
    from agentkit.errors import PolicyViolation
    for fn, params in ((C.xbrl_tieout, {"path": "../facts.csv"}),
                       (C.comps_tie_to_xbrl, {"market_data": "../market_data.csv"}),
                       (C.memo_figures_match, {"path": "//fileserver/share/memo.md"}),
                       (C.no_recommendation_language, {"paths": ["../../memo.md"]}),
                       (C.dataroom_index_complete, {"root": "../"})):
        with pytest.raises(PolicyViolation):
            fn(built, params)


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


def test_eval_cases_are_well_formed(manifest):
    from agentkit.evals import load_cases
    from agentkit.registry import load_specialist
    from agentkit.types import Brief
    milestones = {m["id"] for m in manifest["milestones"]}
    fixtures = PKG / "evals" / "fixtures"
    cases = load_cases(load_specialist("financial-research"))   # the kit's case schema
    assert len(cases) >= 3
    for case in cases:
        assert case.name == case.path.stem and case.milestone in milestones
        assert case.notes
        brief = Brief.from_dict(case.brief)
        assert brief.specialist == "financial-research"
        assert case.fixture is None or (fixtures / case.fixture).is_dir(), case.name
        assert all((fixtures / src).is_file() for src in case.fixtures.values()), case.name
