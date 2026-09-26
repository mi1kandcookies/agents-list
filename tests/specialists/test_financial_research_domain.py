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


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in T.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in T.TOOL_DEFS:
        assert set(d) == {"name", "description", "input_schema", "risk", "function"}
        assert d["risk"] in {"read", "write", "exec", "network", "external"}
        assert d["input_schema"]["type"] == "object" and callable(d["function"])
