"""
tests/specialists/test_appsec_review_domain.py - unit tests for the appsec-review
domain pack: every tool and check (including negative/forged cases), the
manifest parse, and the reference-integrity check that ties agent.yaml's
acceptance checks and tool list back to real kit builtins or this pack's defs.

Fixtures are synthetic; any secret-shaped string is assembled at runtime by
concatenation so nothing secret-looking is ever committed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentkit.errors import ToolError
from specialists.appsec_review import checks as C
from specialists.appsec_review import tools as T

FIXTURES = Path(__file__).resolve().parents[1].parent / "specialists" / "appsec_review" / "evals" / "fixtures"
PKG = Path(__file__).resolve().parents[1].parent / "specialists" / "appsec_review"

# Kit builtin names the plan promises (agentkit checks/builtin.py and tools/).
KIT_BUILTIN_CHECKS = {
    "file_exists", "files_exist", "markdown_sections", "no_placeholders",
    "word_count", "json_valid", "csv_columns", "command_succeeds",
    "ledger_verified", "citations_resolve", "disclaimer_present",
    "rubric_grader", "human_signoff",
}
KIT_BUILTIN_TOOLS = {
    "read_document", "read_file", "write_file", "edit_file", "list_files",
    "search_files", "record_source", "record_claim", "http_fetch",
    "web_search", "ask_client", "post_progress", "submit_milestone",
    "run_command",
}


# --- helpers -----------------------------------------------------------------

def _make_aws_key() -> str:
    """Assemble an AWS-access-key-shaped string at runtime (never committed)."""
    body = ("EXAMPLEKEY" + "ABCDEF1234")[:16]  # 16 uppercase alnum chars
    return "AKIA" + body


def _make_password_value() -> str:
    return "s3cr3t" + "Value" + "12345"  # 16 chars, not a placeholder


def _workspace(tmp_path: Path, *, with_secret: bool = True) -> Path:
    """Materialise the standard workspace (inputs/ repo/ deliverables/)."""
    ws = tmp_path / "ws"
    (ws / "inputs").mkdir(parents=True)
    (ws / "repo").mkdir(parents=True)
    (ws / "deliverables").mkdir(parents=True)
    (ws / "inputs" / "openapi.json").write_text(
        (FIXTURES / "openapi.json").read_text(encoding="utf-8"), encoding="utf-8")
    (ws / "inputs" / "scope.json").write_text(
        (FIXTURES / "scope.json").read_text(encoding="utf-8"), encoding="utf-8")
    (ws / "repo" / "transactions.py").write_text(
        (FIXTURES / "source" / "transactions.py").read_text(encoding="utf-8"), encoding="utf-8")
    (ws / "repo" / "requirements.txt").write_text(
        (FIXTURES / "requirements.txt").read_text(encoding="utf-8"), encoding="utf-8")
    if with_secret:
        secret_file = "\n".join([
            "import os",
            "",
            "# planted for tests",
            'AWS_KEY = "' + _make_aws_key() + '"',
            'password = "' + _make_password_value() + '"',
            "SESSION_SECRET = os.environ['SESSION_SECRET']",
            'PLACEHOLDER = "your-api-key-here"',
            "",
        ])
        (ws / "repo" / "config.py").write_text(secret_file, encoding="utf-8")
    return ws


def _vuln_line(ws: Path) -> tuple[int, str]:
    """1-based line of the SQL-injection sink in the fixture + a snippet of it."""
    lines = (ws / "repo" / "transactions.py").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, start=1):
        if "SELECT * FROM txns WHERE account" in line:
            return i, "SELECT * FROM txns WHERE account"
    raise AssertionError("fixture sink line not found")


# --- scan_secrets ------------------------------------------------------------

def test_scan_secrets_finds_planted_and_redacts(tmp_path):
    ws = _workspace(tmp_path)
    out = T.scan_secrets(ws, root="repo")
    rules = {f["rule_id"] for f in out["findings"]}
    assert "SECRET.AWS_ACCESS_KEY_ID" in rules
    assert "SECRET.GENERIC_ASSIGNMENT" in rules
    assert out["files_scanned"] >= 2
    # The raw secret value never appears in the returned evidence.
    blob = json.dumps(out)
    assert _make_aws_key() not in blob
    assert _make_password_value() not in blob


def test_scan_secrets_ignores_placeholders_and_env(tmp_path):
    ws = _workspace(tmp_path)
    out = T.scan_secrets(ws, root="repo")
    for f in out["findings"]:
        assert "your-api-key-here" not in f["excerpt"]
    # os.environ access must not be reported as a hard-coded secret.
    assert all("SESSION_SECRET = os.environ" not in f.get("excerpt", "")
               for f in out["findings"])


def test_scan_secrets_clean_tree_has_no_findings(tmp_path):
    ws = _workspace(tmp_path, with_secret=False)
    out = T.scan_secrets(ws, root="repo")
    assert out["secret_count"] == 0


def test_scan_secrets_path_escape_rejected(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ToolError):
        T.scan_secrets(ws, root="../../etc")


# --- audit_dependencies ------------------------------------------------------

class _Resp:
    def __init__(self, status, text):
        self.status = status
        self.text = text


def _osv_fetch_factory(vuln_for="flask"):
    def fetch(url, *, method="GET", headers=None, body=None):
        payload = json.loads(body)
        name = payload["package"]["name"]
        if name == vuln_for:
            return _Resp(200, json.dumps({"vulns": [{
                "id": "GHSA-xxxx-yyyy-zzzz",
                "summary": "Example vulnerability in " + name,
                "aliases": ["CVE-2020-00000"],
                "severity": [{"type": "CVSS_V3", "score": "7.5"}],
                "affected": [{"package": {"name": name},
                              "ranges": [{"type": "ECOSYSTEM",
                                          "events": [{"introduced": "0"}, {"fixed": "9.9.9"}]}]}],
            }]}))
        return _Resp(200, json.dumps({"vulns": []}))
    return fetch


def test_audit_dependencies_parses_and_queries(tmp_path):
    ws = _workspace(tmp_path)
    out = T.audit_dependencies(ws, manifest="repo/requirements.txt",
                               fetch=_osv_fetch_factory("flask"))
    assert out["package_count"] == 5
    assert out["vulnerable_count"] == 1
    v = out["vulnerabilities"][0]
    assert v["package"] == "flask"
    assert v["id"] == "GHSA-xxxx-yyyy-zzzz"
    assert v["fixed"] == "9.9.9"
    assert "CVE-2020-00000" in v["aliases"]


def test_audit_dependencies_requires_fetch(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ToolError):
        T.audit_dependencies(ws, manifest="repo/requirements.txt", fetch=None)


def test_audit_dependencies_parses_package_lock(tmp_path):
    ws = _workspace(tmp_path)
    lock = {"lockfileVersion": 3, "packages": {
        "": {"name": "app"},
        "node_modules/lodash": {"version": "4.17.15"},
        "node_modules/express": {"version": "4.16.0"},
    }}
    (ws / "repo" / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    out = T.audit_dependencies(ws, manifest="repo/package-lock.json",
                               fetch=_osv_fetch_factory("lodash"))
    # the per-package list lives in the full report, not in the model's answer
    assert "packages" not in out and out["full_report"] == "deliverables/m2-findings/dependencies.json"
    full = json.loads((ws / out["full_report"]).read_text(encoding="utf-8"))
    names = {p["name"] for p in full["packages"]}
    assert names == {"lodash", "express"}
    assert all(p["ecosystem"] == "npm" for p in full["packages"])
    assert out["vulnerable_count"] == 1 and out["package_count"] == 2


def test_audit_dependencies_missing_manifest(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ToolError):
        T.audit_dependencies(ws, manifest="repo/nope.txt", fetch=_osv_fetch_factory())


# --- parse_openapi -----------------------------------------------------------

def test_parse_openapi_enumerates_and_flags_unauth(tmp_path):
    ws = _workspace(tmp_path)
    out = T.parse_openapi(ws, spec_path="inputs/openapi.json")
    assert out["count"] == 5
    assert "GET /health" in out["unauthenticated"]
    assert "POST /login" in out["unauthenticated"]
    assert out["servers"] == ["https://staging.northwind-ledger.example/api"]
    accounts = next(e for e in out["endpoints"] if e["path"] == "/accounts")
    assert accounts["auth_required"] is True
    assert "limit" in accounts["parameters"] and "cursor" in accounts["parameters"]


def test_parse_openapi_missing_spec(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ToolError):
        T.parse_openapi(ws, spec_path="inputs/nope.json")


# --- cvss_base_score ---------------------------------------------------------

def test_cvss_base_score_critical_vector():
    out = T.cvss_base_score(vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    assert out["base_score"] == 9.8
    assert out["severity"] == "Critical"


def test_cvss_base_score_scope_changed_is_ten():
    out = T.cvss_base_score(vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H")
    assert out["base_score"] == 10.0
    assert out["severity"] == "Critical"


def test_cvss_base_score_no_impact_is_zero():
    out = T.cvss_base_score(vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N")
    assert out["base_score"] == 0.0
    assert out["severity"] == "None"


def test_cvss_base_score_invalid_vectors():
    with pytest.raises(ToolError):
        T.cvss_base_score(vector="")
    with pytest.raises(ToolError):
        T.cvss_base_score(vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/C:H/I:H/A:H")  # no S
    with pytest.raises(ToolError):
        T.cvss_base_score(vector="CVSS:3.1/AV:Z/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")  # bad AV


# --- check_scope -------------------------------------------------------------

def test_check_scope_in_and_out(tmp_path):
    ws = _workspace(tmp_path)
    out = T.check_scope(ws, scope_file="inputs/scope.json", targets=[
        "https://staging.northwind-ledger.example/api/accounts",
        "api.staging.northwind-ledger.example",
        "https://prod.northwind-ledger.example/",
    ])
    verdicts = {r["target"]: r["in_scope"] for r in out["results"]}
    assert verdicts["https://staging.northwind-ledger.example/api/accounts"] is True
    assert verdicts["api.staging.northwind-ledger.example"] is True
    assert verdicts["https://prod.northwind-ledger.example/"] is False
    assert out["all_in_scope"] is False


def test_check_scope_missing_file(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ToolError):
        T.check_scope(ws, scope_file="inputs/nope.json", targets=["x"])


# --- build_sarif -------------------------------------------------------------

def _sample_findings(ws: Path):
    line, snippet = _vuln_line(ws)
    return [{
        "rule_id": "APPSEC.SQLI",
        "name": "SQL injection",
        "message": "User input concatenated into a SQL query.",
        "severity": "high",
        "file": "transactions.py",
        "start_line": line,
        "snippet": snippet,
        "cwe": "CWE-89",
        "owasp": "A03",
        "cvss": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
        "cvss_score": 8.8,
        "exploit": "A crafted accountId of `' OR '1'='1` returns all rows.",
        "remediation": "Use a parameterised query (the ? placeholder form below it).",
    }]


def test_build_sarif_structure(tmp_path):
    ws = _workspace(tmp_path)
    out = T.build_sarif(ws, findings=_sample_findings(ws),
                        output_path="deliverables/m2-findings/findings.sarif")
    assert out["results"] == 1 and out["rules"] == 1
    log = json.loads((ws / out["path"]).read_text(encoding="utf-8"))
    assert log["version"] == "2.1.0"
    run = log["runs"][0]
    assert run["tool"]["driver"]["name"] == "agents-list-appsec"
    res = run["results"][0]
    assert res["ruleId"] == "APPSEC.SQLI"
    assert res["level"] == "error"
    props = res["properties"]
    assert props["cwe"] == "CWE-89" and props["security-severity"] == "8.8"
    region = res["locations"][0]["physicalLocation"]["region"]
    assert region["snippet"]["text"] == "SELECT * FROM txns WHERE account"


def test_build_sarif_rejects_incomplete_finding(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ToolError):
        T.build_sarif(ws, findings=[{"rule_id": "X", "file": "a.py"}])  # no start_line
    with pytest.raises(ToolError):
        T.build_sarif(ws, findings=[{"file": "a.py", "start_line": 1}])  # no rule_id


# --- checks: sarif_valid -----------------------------------------------------

def _write_sarif(ws, findings, path="deliverables/m2-findings/findings.sarif"):
    T.build_sarif(ws, findings=findings, output_path=path)
    return {"path": path}


def test_sarif_valid_passes(tmp_path):
    ws = _workspace(tmp_path)
    params = _write_sarif(ws, _sample_findings(ws))
    res = C.sarif_valid(ws, params)
    assert res["passed"] is True and res["score"] == 1.0


def test_sarif_valid_fails_bad_version(tmp_path):
    ws = _workspace(tmp_path)
    p = ws / "deliverables" / "m2-findings"
    p.mkdir(parents=True)
    (p / "findings.sarif").write_text(json.dumps({"version": "2.0.0", "runs": [
        {"tool": {"driver": {"name": "x", "rules": []}}, "results": []}]}), encoding="utf-8")
    res = C.sarif_valid(ws, {"path": "deliverables/m2-findings/findings.sarif"})
    assert res["passed"] is False
    assert "2.0.0" in res["details"]


def test_sarif_valid_missing_file(tmp_path):
    ws = _workspace(tmp_path)
    res = C.sarif_valid(ws, {"path": "deliverables/m2-findings/findings.sarif"})
    assert res["passed"] is False and res["score"] == 0.0


# --- checks: sarif_locations_exist -------------------------------------------

def test_sarif_locations_exist_passes(tmp_path):
    ws = _workspace(tmp_path)
    params = _write_sarif(ws, _sample_findings(ws))
    res = C.sarif_locations_exist(ws, {**params, "code_root": "repo"})
    assert res["passed"] is True and res["score"] == 1.0


def test_sarif_locations_exist_fails_forged_snippet(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    findings[0]["snippet"] = "this text is not anywhere in the source file"
    params = _write_sarif(ws, findings)
    res = C.sarif_locations_exist(ws, {**params, "code_root": "repo"})
    assert res["passed"] is False


def test_sarif_locations_exist_fails_missing_file(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    findings[0]["file"] = "does_not_exist.py"
    del findings[0]["snippet"]
    params = _write_sarif(ws, findings)
    res = C.sarif_locations_exist(ws, {**params, "code_root": "repo"})
    assert res["passed"] is False
    assert "not found" in res["details"]


def test_sarif_locations_exist_fails_line_out_of_range(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    findings[0]["start_line"] = 99999
    del findings[0]["snippet"]
    params = _write_sarif(ws, findings)
    res = C.sarif_locations_exist(ws, {**params, "code_root": "repo"})
    assert res["passed"] is False


# --- checks: findings_have_evidence ------------------------------------------

def test_findings_have_evidence_passes(tmp_path):
    ws = _workspace(tmp_path)
    params = _write_sarif(ws, _sample_findings(ws))
    res = C.findings_have_evidence(ws, params)
    assert res["passed"] is True


def test_findings_have_evidence_fails_missing_remediation(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    del findings[0]["remediation"]
    params = _write_sarif(ws, findings)
    res = C.findings_have_evidence(ws, params)
    assert res["passed"] is False
    assert "remediation" in res["details"]


# --- checks: scope_respected -------------------------------------------------

def test_scope_respected_source_only_passes(tmp_path):
    ws = _workspace(tmp_path)
    params = _write_sarif(ws, _sample_findings(ws))
    res = C.scope_respected(ws, {**params, "scope_file": "inputs/scope.json"})
    assert res["passed"] is True


def test_scope_respected_out_of_scope_fails(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    findings[0]["file"] = "https://prod.northwind-ledger.example/api/x"
    del findings[0]["snippet"]
    params = _write_sarif(ws, findings)
    res = C.scope_respected(ws, {**params, "scope_file": "inputs/scope.json"})
    assert res["passed"] is False
    assert "out of scope" in res["details"] or "out-of-scope" in res["details"].lower()


def test_scope_respected_in_scope_host_passes(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    findings[0]["file"] = "https://staging.northwind-ledger.example/api/x"
    del findings[0]["snippet"]
    params = _write_sarif(ws, findings)
    res = C.scope_respected(ws, {**params, "scope_file": "inputs/scope.json"})
    assert res["passed"] is True


def test_scope_respected_no_scope_file_pending(tmp_path):
    ws = _workspace(tmp_path)
    params = _write_sarif(ws, _sample_findings(ws))
    res = C.scope_respected(ws, {**params, "scope_file": "inputs/nope.json"})
    assert res["passed"] is None


# --- checks: secret_findings_reconcile ---------------------------------------

def _write_secret_report(ws, findings, path="deliverables/m2-findings/secrets.json"):
    out = ws / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings), encoding="utf-8")
    return {"report": path, "code_root": "repo"}


def test_secret_findings_reconcile_passes(tmp_path):
    ws = _workspace(tmp_path)
    real = T.scan_secrets(ws, root="repo")["findings"]
    params = _write_secret_report(ws, real)
    res = C.secret_findings_reconcile(ws, params)
    assert res["passed"] is True and res["score"] == 1.0


def test_secret_findings_reconcile_fails_forged(tmp_path):
    ws = _workspace(tmp_path)
    forged = [{"file": "repo_ghost.py", "line": 4, "rule_id": "SECRET.AWS_ACCESS_KEY_ID"}]
    params = _write_secret_report(ws, forged)
    res = C.secret_findings_reconcile(ws, params)
    assert res["passed"] is False
    assert "unverifiable" in res["details"]


def test_secret_findings_reconcile_empty_passes(tmp_path):
    ws = _workspace(tmp_path, with_secret=False)
    params = _write_secret_report(ws, [])
    res = C.secret_findings_reconcile(ws, params)
    assert res["passed"] is True


def test_secret_findings_reconcile_missing_report(tmp_path):
    ws = _workspace(tmp_path)
    res = C.secret_findings_reconcile(ws, {"report": "deliverables/m2-findings/nope.json",
                                           "code_root": "repo"})
    assert res["passed"] is False


# --- kit integration: jails, authored files, ledger sources ------------------

def _mark_authored(ws: Path, *rels: str) -> None:
    from agentkit.ledger import Ledger
    ledger = Ledger(ws)
    for rel in rels:
        ledger.note_authored(rel)


def test_build_sarif_uri_is_relative_to_code_root(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    findings[0]["file"] = "repo/transactions.py"      # as scan_secrets reports paths
    params = _write_sarif(ws, findings)
    log = json.loads((ws / params["path"]).read_text(encoding="utf-8"))
    uri = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "transactions.py"
    assert C.sarif_locations_exist(ws, {**params, "code_root": "repo"})["passed"] is True


def test_build_sarif_output_path_is_jailed(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ToolError):
        T.build_sarif(ws, findings=_sample_findings(ws), output_path="../escape.sarif")
    assert not (tmp_path / "escape.sarif").exists()


def test_scan_secrets_skips_kit_internals_and_graders_only_inner_dirs(tmp_path):
    # A workspace that itself lives under a "build" directory is still scanned.
    ws = _workspace(tmp_path / "build")
    (ws / ".agentkit").mkdir()
    (ws / ".agentkit" / "journal.py").write_text('KEY = "' + _make_aws_key() + '"\n', encoding="utf-8")
    out = T.scan_secrets(ws, root=".")
    files = {f["file"] for f in out["findings"]}
    assert "repo/config.py" in files
    assert not any(f.startswith(".agentkit/") for f in files)


def test_sarif_locations_exist_rejects_escape_from_code_root(tmp_path):
    ws = _workspace(tmp_path)
    findings = _sample_findings(ws)
    findings[0]["file"] = "../inputs/scope.json"
    findings[0]["start_line"] = 2
    del findings[0]["snippet"]
    params = _write_sarif(ws, findings)
    res = C.sarif_locations_exist(ws, {**params, "code_root": "repo"})
    assert res["passed"] is False and "code root" in res["details"]


def test_sarif_locations_exist_rejects_agent_written_file(tmp_path):
    ws = _workspace(tmp_path)
    planted = "def handler(q):\n    return db.execute('SELECT * FROM t WHERE x = ' + q)\n"
    (ws / "repo" / "planted.py").write_text(planted, encoding="utf-8")
    _mark_authored(ws, "repo/planted.py")
    findings = _sample_findings(ws)
    findings[0].update(file="planted.py", start_line=2, snippet="SELECT * FROM t WHERE x = ")
    params = _write_sarif(ws, findings)
    res = C.sarif_locations_exist(ws, {**params, "code_root": "repo"})
    assert res["passed"] is False and "written by the agent" in res["details"]


def test_secret_findings_reconcile_ignores_agent_written_secrets(tmp_path):
    ws = _workspace(tmp_path, with_secret=False)
    (ws / "repo" / "settings.py").write_text('password = "' + _make_password_value() + '"\n',
                                             encoding="utf-8")
    _mark_authored(ws, "repo/settings.py")
    claimed = T.scan_secrets(ws, root="repo")["findings"]
    assert claimed, "the planted value should look like a secret to the scanner"
    res = C.secret_findings_reconcile(ws, _write_secret_report(ws, claimed))
    assert res["passed"] is False and "unverifiable" in res["details"]


def test_secret_findings_reconcile_rejects_malformed_items(tmp_path):
    ws = _workspace(tmp_path)
    params = _write_secret_report(ws, [{"file": "repo/config.py", "line": "4",
                                        "rule_id": "SECRET.AWS_ACCESS_KEY_ID"}, "junk"])
    res = C.secret_findings_reconcile(ws, params)
    assert res["passed"] is False and res["score"] == 0.0


def test_checks_refuse_params_outside_workspace(tmp_path):
    ws = _workspace(tmp_path)
    assert C.sarif_valid(ws, {"path": "../../outside.sarif"})["passed"] is False
    res = C.secret_findings_reconcile(ws, {"report": "../x.json", "code_root": "repo"})
    assert res["passed"] is False and "refused" in res["details"]


def test_audit_dependencies_registers_osv_sources(tmp_path):
    from agentkit.ledger import Ledger
    ws = _workspace(tmp_path)
    ledger = Ledger(ws)
    out = T.audit_dependencies(ws, manifest="repo/requirements.txt",
                               fetch=_osv_fetch_factory("flask"), ledger=ledger)
    vuln = out["vulnerabilities"][0]
    src = ledger.source(vuln["source"])
    assert src is not None and src.kind == "tool" and src.uri == "osv:PyPI/flask@2.0.1"
    assert "GHSA-xxxx-yyyy-zzzz" in ledger.snapshot(src.id)
    assert len(ledger.sources) == 1          # packages without advisories add no source


# --- tool/check registries ---------------------------------------------------

def test_tool_defs_wellformed():
    names = set()
    for d in T.TOOL_DEFS:
        assert {"name", "description", "input_schema", "risk", "function"} <= set(d)
        assert callable(d["function"])
        assert d["risk"] in {"read", "write", "exec", "network", "external"}
        assert isinstance(d["input_schema"], dict)
        names.add(d["name"])
    assert len(names) == len(T.TOOL_DEFS)  # unique names


def test_check_defs_wellformed():
    assert C.CHECK_DEFS, "no checks defined"
    for name, fn in C.CHECK_DEFS.items():
        assert callable(fn)


# --- manifest ----------------------------------------------------------------

def _load_manifest() -> dict:
    with open(PKG / "agent.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_agent_yaml_parses_and_has_core_fields():
    m = _load_manifest()
    assert m["schema_version"] == 1
    assert m["slug"] == "appsec-review"
    assert m["profile"] == "code"
    assert m["human_gate"]["required"] is True
    assert m["human_gate"]["reviewer_role"] == "customer security lead"
    assert m["egress"]["mode"] == "allowlist"
    assert "api.osv.dev" in m["egress"]["allow"]
    assert len(m["milestones"]) == 3
    assert m["listing"]["pricing"]["model"] == "per_milestone"
    assert m["models"]["primary"].startswith("anthropic:")


def test_manifest_deliverables_under_milestone_dirs():
    m = _load_manifest()
    for ms in m["milestones"]:
        assert ms["deliverables"], f"{ms['id']} has no deliverables"
        for d in ms["deliverables"]:
            assert d.startswith(f"deliverables/{ms['id']}/"), d


def test_manifest_checks_are_known():
    m = _load_manifest()
    known = KIT_BUILTIN_CHECKS | set(C.CHECK_DEFS)
    for ms in m["milestones"]:
        assert ms["acceptance"], f"{ms['id']} has no acceptance criteria"
        for crit in ms["acceptance"]:
            assert crit["check"] in known, f"unknown check: {crit['check']}"


def test_manifest_tools_are_known():
    m = _load_manifest()
    known = KIT_BUILTIN_TOOLS | {d["name"] for d in T.TOOL_DEFS}
    for tool in m["tools"]:
        assert tool in known, f"unknown tool: {tool}"
    # Every domain tool this pack ships is wired into the manifest.
    for d in T.TOOL_DEFS:
        assert d["name"] in m["tools"], f"tool {d['name']} missing from manifest"


def test_manifest_rubric_files_exist():
    m = _load_manifest()
    for ms in m["milestones"]:
        for crit in ms["acceptance"]:
            if crit["check"] == "rubric_grader":
                rubric = PKG / crit["params"]["rubric"]
                assert rubric.exists(), f"missing rubric: {rubric}"
                data = yaml.safe_load(rubric.read_text(encoding="utf-8"))
                assert "criteria" in data and "threshold" in data
                assert abs(sum(c["weight"] for c in data["criteria"]) - 1.0) < 1e-6


def test_human_signoff_present_in_every_milestone():
    m = _load_manifest()
    for ms in m["milestones"]:
        kinds = {crit.get("kind", "automated") for crit in ms["acceptance"]}
        assert "human" in kinds, f"{ms['id']} lacks a human gate"


# --- eval cases --------------------------------------------------------------

def test_eval_cases_parse_and_reference_real_milestones():
    m = _load_manifest()
    milestone_ids = {ms["id"] for ms in m["milestones"]}
    case_dir = PKG / "evals" / "cases"
    cases = sorted(case_dir.glob("*.json"))
    assert len(cases) == 3
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert {"name", "brief", "milestone", "notes"} <= set(case)
        assert case["milestone"] in milestone_ids
        assert case["brief"]["specialist"] == "appsec-review"
