"""
tests/specialists/test_appsec_review_hardening.py - regression tests for the
appsec-review pack's verification rules: what the secret scanner covers and
reports skipping, how an OSV range picks the fix, what a requirements file
leaves unaudited, the scope rules, CVSS vector strictness, the scores the
SARIF writer derives, and the checks that recompute all of these instead of
trusting the agent.

Secret-shaped strings are assembled at runtime so nothing secret-looking is
committed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentkit.checks import CheckContext
from agentkit.checks.builtin import no_placeholders
from agentkit.errors import ToolError
from agentkit.ledger import Ledger
from agentkit.registry import load_specialist
from agentkit.tools import truncate
from specialists.appsec_review import checks as C
from specialists.appsec_review import tools as T

PKG = Path(__file__).resolve().parents[2] / "specialists" / "appsec_review"
FIXTURES = PKG / "evals" / "fixtures"
SARIF = "deliverables/m2-findings/findings.sarif"
SQLI_VECTOR = "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H"      # 8.8 High
LOW_VECTOR = "CVSS:3.1/AV:N/AC:H/PR:L/UI:R/S:U/C:L/I:N/A:N"       # 2.6 Low


def _tok(*parts: str) -> str:
    return "".join(parts)


PW = _tok("Tr0ub4dor", "-and-3")


def _aws_key() -> str:
    return "AKIA" + ("EXAMPLEKEY" + "ABCDEF1234")[:16]


def _write(ws: Path, rel: str, text: str) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    for d in ("inputs", "repo", "deliverables"):
        (ws / d).mkdir(parents=True)
    for name in ("scope.json", "openapi.json"):
        _write(ws, f"inputs/{name}", (FIXTURES / name).read_text(encoding="utf-8"))
    _write(ws, "repo/transactions.py", (FIXTURES / "source" / "transactions.py").read_text(encoding="utf-8"))
    return ws


def _sqli_line(ws: Path) -> int:
    lines = (ws / "repo" / "transactions.py").read_text(encoding="utf-8").splitlines()
    return next(i for i, line in enumerate(lines, 1) if "SELECT * FROM txns WHERE account = '" in line)


def _finding(ws: Path, **kw) -> dict:
    base = {"rule_id": "APPSEC.SQLI", "name": "SQL injection", "message": "account_id reaches SQL.",
            "file": "transactions.py", "start_line": _sqli_line(ws),
            "snippet": "SELECT * FROM txns WHERE account = '", "cwe": "CWE-89", "owasp": "A03",
            "cvss": SQLI_VECTOR, "exploit": "account_id = x' OR '1'='1", "remediation": "Bind parameters."}
    return {**base, **kw}


def _sarif(ws: Path, findings: list[dict]) -> dict:
    T.build_sarif(ws, findings=findings, output_path=SARIF)
    return json.loads((ws / SARIF).read_text(encoding="utf-8"))


def _save(ws: Path, log: dict) -> None:
    (ws / SARIF).write_text(json.dumps(log), encoding="utf-8")


class _Resp:
    def __init__(self, status, text):
        self.status, self.text = status, text


# --- scan_secrets --------------------------------------------------------------

def test_scanner_finds_common_credential_forms(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "repo/settings.py", "\n".join([
        f'SECRET_KEY = "{PW}a"',
        f'db_password = "{PW}b"',
        f'DB_PASSWORD = "{PW}c"',
        f'client_secret = "{PW}d"',
        f'GITHUB_TOKEN = "{PW}e"',
        f'token := "{PW}f"',
        "TOKEN_URL = 'https://auth.example.com/oauth/token'",       # a URL, not a secret
        "password = os.environ['DB_PASSWORD']",                     # an indirection
        "",
    ]))
    _write(ws, "repo/config.json", '{"db": {"password": "' + PW + 'g", "apiKey": "' + PW + 'h"}}\n')
    _write(ws, "repo/deploy.yaml", "db:\n  password: " + PW + "i\n  token: ${DEPLOY_TOKEN}\n")
    _write(ws, "repo/.env.production", "API_KEY=" + PW + "j\nDEBUG=false\n")
    _write(ws, "repo/app.properties", "spring.datasource.password=" + PW + "k\n")
    key_body = _tok("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "BKcwggSjAgEAAoIBAQC7")
    _write(ws, "repo/deploy.pem", "-----BEGIN " + "PRIVATE KEY-----\n" + key_body + "\n-----END PRIVATE KEY-----\n")
    _write(ws, "repo/tokens.txt", "\n".join([
        _tok("gh", "p_") + "A" * 36,
        _tok("sk_", "live_") + "B" * 24,
        _tok("AI", "za") + "C" * 35,
        "postgres://ledger:" + PW + "l@db.internal:5432/ledger",
        "postgres://user:password@localhost/db",                   # documented placeholder
        "",
    ]))
    out = T.scan_secrets(ws, root="repo")
    got = {(f["file"], f["line"], f["rule_id"]) for f in out["findings"]}
    assert got == {("repo/settings.py", n, "SECRET.GENERIC_ASSIGNMENT") for n in range(1, 7)} | {
        ("repo/config.json", 1, "SECRET.GENERIC_ASSIGNMENT"),
        ("repo/deploy.yaml", 2, "SECRET.GENERIC_ASSIGNMENT"),
        ("repo/.env.production", 1, "SECRET.GENERIC_ASSIGNMENT"),
        ("repo/app.properties", 1, "SECRET.GENERIC_ASSIGNMENT"),
        ("repo/deploy.pem", 1, "SECRET.PRIVATE_KEY_BLOCK"),
        ("repo/tokens.txt", 1, "SECRET.GITHUB_TOKEN"),
        ("repo/tokens.txt", 2, "SECRET.STRIPE_LIVE_KEY"),
        ("repo/tokens.txt", 3, "SECRET.GOOGLE_API_KEY"),
        ("repo/tokens.txt", 4, "SECRET.URL_CREDENTIALS"),
    }
    assert sum(1 for f in out["findings"] if f["file"] == "repo/config.json") == 2
    blob = json.dumps(out)
    assert PW not in blob and key_body not in blob and "A" * 36 not in blob


def test_scanner_reports_one_hit_per_value(tmp_path):
    """A provider key assigned to a keyword variable is one finding, not two."""
    ws = _ws(tmp_path)
    _write(ws, "repo/aws.py", f'AWS_ACCESS_KEY_ID = "{_aws_key()}"\n')
    out = T.scan_secrets(ws, root="repo")
    assert [f["rule_id"] for f in out["findings"]] == ["SECRET.AWS_ACCESS_KEY_ID"]


def test_scanner_reports_what_it_skips(tmp_path):
    ws = _ws(tmp_path)
    (ws / "repo" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (ws / "repo" / "blob.dat").write_bytes(b"ab\x00cd")
    _write(ws, "repo/node_modules/pkg/index.js", f'password = "{PW}"\n')
    out = T.scan_secrets(ws, root="repo")
    assert {s["file"]: s["reason"] for s in out["skipped"]} == {
        "repo/blob.dat": "binary content",
        "repo/logo.png": "binary file type",
        "repo/node_modules/": "vendored, generated or VCS directory",
    }
    assert out["files_scanned"] == 1 and out["skipped_count"] == 3 and out["secret_count"] == 0


# --- dependency audit ------------------------------------------------------------

def _pkg(name: str, version: str, eco: str = "PyPI") -> dict:
    return {"name": name, "version": version, "ecosystem": eco}


def test_osv_fix_is_on_the_installed_branch():
    multi = {"affected": [{"package": {"name": "flask", "ecosystem": "PyPI"}, "ranges": [
        {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.2.5"},
                                         {"introduced": "2.3.0"}, {"fixed": "2.3.2"}]}]}]}
    assert T._osv_fix(multi, _pkg("flask", "2.3.1")) == ("2.3.2", ["2.2.5", "2.3.2"])   # never 2.2.5
    assert T._osv_fix(multi, _pkg("Flask", "2.2.4"))[0] == "2.2.5"                      # PyPI names normalise
    semver = {"affected": [{"package": {"name": "lib"}, "ranges": [
        {"type": "SEMVER", "events": [{"fixed": "1.10.0"}, {"introduced": "1.2.0-beta.1"}]}]}]}
    assert T._osv_fix(semver, _pkg("lib", "1.9.3", "npm"))[0] == "1.10.0"               # numeric, not string order
    assert T._osv_fix(semver, _pkg("lib", "1.2.0-beta.2", "npm"))[0] == "1.10.0"
    no_fix = {"affected": [{"package": {"name": "lib"}, "ranges": [
        {"type": "ECOSYSTEM", "events": [{"introduced": "1.0"}, {"last_affected": "1.4"}]}]}]}
    assert T._osv_fix(no_fix, _pkg("lib", "1.2")) == ("", [])


@pytest.mark.parametrize("lower,higher", [("1.0.dev1", "1.0a1"), ("1.0a1", "1.0b2"), ("1.0rc1", "1.0"),
                                          ("1.0", "1.0.post1"), ("2.9", "2.10"), ("3.0", "1!0.5")])
def test_version_order(lower, higher):
    assert T._version_key(lower) < T._version_key(higher)
    assert T._version_key("1.0") == T._version_key("1.0.0")


def test_osv_severity_is_a_label_with_typed_vectors():
    label, vectors = T._osv_severity({"severity": [
        {"type": "CVSS_V2", "score": "AV:N/AC:L/Au:N/C:P/I:P/A:P"},
        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]})
    assert label == "CRITICAL" and [v["type"] for v in vectors] == ["CVSS_V3", "CVSS_V2"]
    assert T._osv_severity({"database_specific": {"severity": "moderate"}}) == ("MODERATE", [])


def test_requirements_parsing_reports_what_it_cannot_audit():
    text = "\n".join([
        "requests[socks]==2.25.1",
        "urllib3>=1.26,<2",
        "Django~=3.2.0",
        "flask==2.0.1 ; python_version >= '3.8'",
        "cryptography==3.2 \\",
        "    --hash=sha256:" + "a" * 64,
        "-r base.txt",
        "-e ./libs/internal",
        "git+https://example.invalid/acme/lib.git#egg=lib",
        "numpy",
        "--index-url https://pypi.acme.example/simple",
        "# a comment",
    ])
    pkgs, unaudited, indexes = T._parse_requirements(text)
    assert [(p["name"], p["version"]) for p in pkgs] == [
        ("requests", "2.25.1"), ("flask", "2.0.1"), ("cryptography", "3.2")]
    assert {u["line"]: u["reason"] for u in unaudited} == {
        2: "version range, not a pinned version",
        3: "version range, not a pinned version",
        7: "included file not followed: audit it separately",
        8: "editable or local install",
        9: "URL or local path, not a registry version",
        10: "no version pinned",
    }
    assert indexes == ["--index-url https://pypi.acme.example/simple"]


def test_audit_output_survives_the_kit_truncation(tmp_path):
    """Advisories come first, so a large lockfile still gets every one of
    them past the kit's output cap; the full package list goes to a file."""
    ws = _ws(tmp_path)
    packages = {f"node_modules/pkg-{i:04d}": {
        "version": "1.0.0", "resolved": f"https://registry.npmjs.org/pkg-{i:04d}/-/pkg-{i:04d}-1.0.0.tgz"}
        for i in range(1200)}
    _write(ws, "repo/package-lock.json", json.dumps({"lockfileVersion": 3, "packages": packages}))

    def fetch(url, *, method="GET", headers=None, body=None):
        n = int(json.loads(body)["package"]["name"].split("-")[1])
        vulns = [{"id": f"GHSA-{n:04d}-aaaa-bbbb", "summary": "x" * 150}] if n % 25 == 0 else []
        return _Resp(200, json.dumps({"vulns": vulns}))

    out = T.audit_dependencies(ws, manifest="repo/package-lock.json", fetch=fetch)
    assert out["package_count"] == 1200 and out["advisory_count"] == 48
    seen = truncate(json.dumps(out))
    assert all(f"GHSA-{n:04d}-aaaa-bbbb" in seen for n in range(0, 1200, 25))
    full = json.loads((ws / out["full_report"]).read_text(encoding="utf-8"))
    assert len(full["packages"]) == 1200


def test_audit_never_sends_private_or_excluded_packages(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "repo/package-lock.json", json.dumps({"packages": {
        "node_modules/@acme/auth": {"version": "1.0.0",
                                    "resolved": "https://npm.acme.example/@acme/auth/-/auth-1.0.0.tgz"},
        "node_modules/acme-billing": {"version": "3.1.0"},
        "node_modules/lodash": {"version": "4.17.15"},
    }}))
    sent = []

    def fetch(url, *, method="GET", headers=None, body=None):
        sent.append(json.loads(body)["package"]["name"])
        return _Resp(200, '{"vulns": []}')

    out = T.audit_dependencies(ws, manifest="repo/package-lock.json", fetch=fetch, exclude=["acme-*"])
    assert sent == ["lodash"]
    assert {u["package"] for u in out["unaudited"]} == {"@acme/auth", "acme-billing"}


# --- parse_openapi -----------------------------------------------------------------

def test_parse_openapi_optional_auth_yaml_and_refs(tmp_path):
    ws = _ws(tmp_path)
    spec = {"openapi": "3.0.3", "security": [{"bearerAuth": []}],
            "components": {"parameters": {"Limit": {"name": "limit", "in": "query"}}},
            "paths": {"/feed": {"get": {"security": [{}]}},
                      "/items": {"get": {"security": [{"bearerAuth": []}, {}],
                                         "parameters": [{"$ref": "#/components/parameters/Limit"}]}},
                      "/me": {"get": {}}}}
    _write(ws, "inputs/api.yaml", yaml.safe_dump(spec))
    out = T.parse_openapi(ws, spec_path="inputs/api.yaml")
    assert out["unauthenticated"] == ["GET /feed", "GET /items"]      # {} allows anonymous access
    assert next(e for e in out["endpoints"] if e["path"] == "/items")["parameters"] == ["limit"]
    assert next(e for e in out["endpoints"] if e["path"] == "/me")["auth_required"] is True


# --- CVSS ------------------------------------------------------------------------------

@pytest.mark.parametrize("vector", [
    "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",                     # no version prefix
    "CVSS:2.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    "CVSS:3.1/AV:N/AV:P/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",         # a metric twice
    "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/XX:Y",         # unknown metric
    "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/",
])
def test_cvss_rejects_malformed_vectors(vector):
    with pytest.raises(ToolError):
        T.cvss_base_score(vector=vector)


def test_cvss_accepts_3_0_and_temporal_metrics():
    assert T.cvss_base_score(vector="CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")["base_score"] == 9.8
    out = T.cvss_base_score(vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/E:P/RL:O")
    assert out["base_score"] == 9.8 and out["version"] == "3.1"


# --- scope -----------------------------------------------------------------------------

def test_scope_rules(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "inputs/scope.json", json.dumps({
        "hosts": ["*.northwind.example", "shop.example", "10.20.0.0/16"],
        "out_of_scope": ["prod.northwind.example", "anything run by the payment processor"],
        "repos": ["https://github.com/northwind/ledger"]}))
    targets = ["https://prod.northwind.example/", "api.northwind.example", "northwind.example",
               "shop.example", "payments.shop.example", "http://10.20.3.4:8080/x",
               "https://github.com/northwind/ledger/blob/main/app.py", "https://github.com/other/repo"]
    out = T.check_scope(ws, targets=targets)
    assert [(r["in_scope"], r["reason"]) for r in out["results"]] == [
        (False, "listed out of scope"),            # out_of_scope wins over *.northwind.example
        (True, "matches allow-list"),
        (False, "not in allow-list"),              # *.dom does not cover dom itself
        (True, "matches allow-list"),
        (False, "not in allow-list"),              # an exact host does not cover subdomains
        (True, "matches allow-list"),
        (True, "matches an in-scope repository"),
        (False, "not in allow-list"),
    ]
    assert out["unparsed_out_of_scope"] == ["anything run by the payment processor"]

    _write(ws, "inputs/scope.json", json.dumps({"hosts": "shop.example"}))    # a string, not a list
    with pytest.raises(ToolError, match="must be a list"):
        T.check_scope(ws, targets=["shop.example"])


def test_scope_respected_reads_every_deliverable(tmp_path):
    ws = _ws(tmp_path)
    doc = _write(ws, "deliverables/report.md", "\n".join([
        "# Report", "## Scope",
        "In scope: staging.northwind-ledger.example. Out of scope: prod.northwind-ledger.example.",
        "### Exclusions", "Nothing on prod.northwind-ledger.example was touched.",
        "## Findings", "See https://cwe.mitre.org/data/definitions/89.html and transactions.py.", ""]))
    params = {"paths": ["deliverables/report.md"], "scope_file": "inputs/scope.json"}
    res = C.scope_respected(ws, params)
    assert res["passed"] is True and "cwe.mitre.org" in res["details"]
    assert "transactions.py" not in res["details"]          # a file name is not a host

    doc.write_text(doc.read_text(encoding="utf-8") + "Retest on prod.northwind-ledger.example.\n",
                   encoding="utf-8")
    res = C.scope_respected(ws, params)
    assert res["passed"] is False and "prod.northwind-ledger.example (listed out of scope)" in res["details"]

    _write(ws, "deliverables/scope-note.md", "## Scope\nAlso reviewed: ops.northwind-ledger.example\n")
    res = C.scope_respected(ws, {**params, "paths": ["deliverables/scope-note.md"]})
    assert res["passed"] is False and "named in the Scope section but not in the signed scope file" in res["details"]

    _write(ws, "deliverables/surface.json", json.dumps({"servers": ["https://api.partner.example/v1"]}))
    res = C.scope_respected(ws, {**params, "paths": ["deliverables/surface.json"]})
    assert res["passed"] is False and "api.partner.example (not in allow-list)" in res["details"]


# --- SARIF writer ----------------------------------------------------------------------

def test_build_sarif_derives_scores_from_the_vector(tmp_path):
    ws = _ws(tmp_path)
    log = _sarif(ws, [_finding(ws), _finding(ws, message="second", cvss=LOW_VECTOR, severity="low")])
    run = log["runs"][0]
    first, second = run["results"]
    assert (first["level"], first["properties"]["security-severity"]) == ("error", "8.8")
    assert (second["level"], second["properties"]["security-severity"]) == ("note", "2.6")
    rule = run["tool"]["driver"]["rules"][0]
    assert rule["properties"]["security-severity"] == "8.8"               # the rule's highest score
    assert rule["properties"]["tags"] == ["security", "external/cwe/cwe-89"]
    assert rule["defaultConfiguration"]["level"] == "error"


@pytest.mark.parametrize("override,message", [
    ({"cvss_score": 2.0}, "does not match its vector"),
    ({"severity": "critical"}, "does not match the vector's band"),
    ({"cvss": None, "cvss_score": 8.8}, "needs the cvss vector"),
    ({"cvss": "AV:N/AC:L"}, "must start with CVSS:3.1"),
])
def test_build_sarif_refuses_scores_the_vector_does_not_give(tmp_path, override, message):
    ws = _ws(tmp_path)
    with pytest.raises(ToolError, match=message):
        T.build_sarif(ws, findings=[_finding(ws, **override)], output_path=SARIF)


def test_build_sarif_keeps_secret_values_out(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "repo/config.py", f'AWS_ACCESS_KEY_ID = "{_aws_key()}"\n')
    secret = _finding(ws, rule_id="SECRET.AWS_ACCESS_KEY_ID", file="repo/config.py", start_line=1,
                      snippet=f'AWS_ACCESS_KEY_ID = "{_aws_key()}"', cwe="CWE-798",
                      cvss="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
                      message=f"Key {_aws_key()} is hard-coded.", exploit=f"aws --key {_aws_key()}")
    T.build_sarif(ws, findings=[secret], output_path=SARIF)
    text = (ws / SARIF).read_text(encoding="utf-8")
    assert _aws_key() not in text and '"snippet"' not in text
    assert "Key AKIA***EF (len 20) is hard-coded." in text
    res = C.sarif_locations_exist(ws, {"path": SARIF, "code_root": "repo"})
    assert res["passed"] is True and "1 by a fresh secret scan" in res["details"]


# --- checks: locations, evidence, CVSS -------------------------------------------------

def test_sarif_locations_need_real_evidence(tmp_path):
    ws = _ws(tmp_path)
    params = {"path": SARIF, "code_root": "repo"}
    _sarif(ws, [_finding(ws, start_line=1, snippet=None)])            # the module docstring, no snippet
    res = C.sarif_locations_exist(ws, params)
    assert res["passed"] is False and "needs a source snippet" in res["details"]

    _sarif(ws, [_finding(ws, snippet="SELECT")])                        # too short to prove anything
    assert C.sarif_locations_exist(ws, params)["passed"] is False

    _sarif(ws, [_finding(ws, rule_id="SECRET.AWS_ACCESS_KEY_ID", snippet=None, cwe="CWE-798")])
    res = C.sarif_locations_exist(ws, params)                           # no key on that line
    assert res["passed"] is False and "fresh secret scan" in res["details"]

    _sarif(ws, [])
    res = C.sarif_locations_exist(ws, params)
    assert res["passed"] is True and "no findings" in res["details"]


def test_findings_need_a_cvss_vector(tmp_path):
    ws = _ws(tmp_path)
    _sarif(ws, [_finding(ws, cvss=None)])
    res = C.findings_have_evidence(ws, {"path": SARIF})
    assert res["passed"] is False and "cvss vector" in res["details"]


def test_cvss_consistent_recomputes_levels_and_scores(tmp_path):
    ws = _ws(tmp_path)
    log = _sarif(ws, [_finding(ws)])
    assert C.cvss_consistent(ws, {"path": SARIF})["passed"] is True

    log["runs"][0]["results"][0]["level"] = "warning"
    _save(ws, log)
    res = C.cvss_consistent(ws, {"path": SARIF})
    assert res["passed"] is False and "level 'warning' should be 'error'" in res["details"]

    log["runs"][0]["results"][0]["level"] = "error"
    log["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["security-severity"] = "9.9"
    _save(ws, log)
    res = C.cvss_consistent(ws, {"path": SARIF})
    assert res["passed"] is False and "rule APPSEC.SQLI: security-severity '9.9' should be 8.8" in res["details"]

    del log["runs"][0]["results"][0]["properties"]["cvss"]
    _save(ws, log)
    assert "no CVSS vector" in C.cvss_consistent(ws, {"path": SARIF})["details"]


# --- checks: secrets -------------------------------------------------------------------

def _secrets_report(ws: Path, report) -> dict:
    _write(ws, "deliverables/m2-findings/secrets.json", json.dumps(report))
    return {"report": "deliverables/m2-findings/secrets.json", "code_root": "repo"}


def test_secret_reconcile_requires_every_hit_accounted_for(tmp_path):
    ws = _ws(tmp_path)
    _write(ws, "repo/config.py", f'AWS_ACCESS_KEY_ID = "{_aws_key()}"\n')
    res = C.secret_findings_reconcile(ws, _secrets_report(ws, []))
    assert res["passed"] is False and "repo/config.py:1 [SECRET.AWS_ACCESS_KEY_ID]" in res["details"]

    hit = {"file": "repo/config.py", "line": 1, "rule_id": "SECRET.AWS_ACCESS_KEY_ID"}
    res = C.secret_findings_reconcile(ws, _secrets_report(ws, {"findings": [], "dismissed": [hit]}))
    assert res["passed"] is False and "no reason" in res["details"]

    ghost = {**hit, "line": 9, "reason": "test data"}
    res = C.secret_findings_reconcile(ws, _secrets_report(ws, {"findings": [hit], "dismissed": [ghost]}))
    assert res["passed"] is False and "match no hit" in res["details"]

    res = C.secret_findings_reconcile(ws, _secrets_report(ws, {"findings": [], "dismissed": [
        {**hit, "reason": "rotated key kept as a regression fixture"}]}))
    assert res["passed"] is True and "rotated key kept as a regression fixture" in res["details"]


def test_no_secret_values_catches_a_pasted_secret(tmp_path):
    ws = _ws(tmp_path)
    key_body = _tok("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "BKcwggSjAgEAAoIBAQC7")
    _write(ws, "repo/deploy.pem", "-----BEGIN " + "PRIVATE KEY-----\n" + key_body + "\n")
    _write(ws, "inputs/prod.env", "DB_PASSWORD=" + PW + "\n")
    _write(ws, "deliverables/m2-findings/findings.md", "Found a private key block in repo/deploy.pem.\n")
    assert C.no_secret_values(ws, {"path": "deliverables"})["passed"] is True

    _write(ws, "deliverables/m3-report/report.md", f"Key starts {key_body}; password {PW}.\n")
    res = C.no_secret_values(ws, {"path": "deliverables"})
    assert res["passed"] is False
    assert "SECRET.PRIVATE_KEY_BLOCK value from repo/deploy.pem:1" in res["details"]
    assert "SECRET.GENERIC_ASSIGNMENT value from inputs/prod.env:1" in res["details"]
    assert PW not in res["details"] and key_body not in res["details"]


# --- checks: advisories ----------------------------------------------------------------

def test_advisories_reconcile_against_recorded_osv_answers(tmp_path):
    ws = _ws(tmp_path)
    ledger = Ledger(ws)
    ledger.add_source("osv:PyPI/flask@2.0.1", "OSV", json.dumps({"vulns": [
        {"id": "PYSEC-2099-0001", "aliases": ["CVE-2099-0001", "GHSA-abcd-efgh-ijkm"]}]}), kind="tool")
    ledger.add_source("workspace:repo/CHANGELOG.md", "changelog", "fixes CVE-2099-7777", kind="customer")
    doc = _write(ws, "deliverables/findings.md",
                 "flask is affected by PYSEC-2099-0001 (cve-2099-0001, GHSA-ABCD-EFGH-IJKM).\n")
    params = {"paths": ["deliverables/findings.md"]}
    assert C.advisories_reconcile(ws, params)["passed"] is True

    doc.write_text(doc.read_text(encoding="utf-8") + "Also CVE-2099-7777 and CVE-2099-00011.\n",
                   encoding="utf-8")
    res = C.advisories_reconcile(ws, params)
    assert res["passed"] is False                       # a customer file is not an advisory source
    assert "CVE-2099-7777" in res["details"] and "CVE-2099-00011" in res["details"]

    # a snapshot edited after it was recorded no longer counts
    doc.write_text("Only CVE-2099-0001.\n", encoding="utf-8")
    ledger.snapshot_path("S1").write_text("{}", encoding="utf-8")
    assert C.advisories_reconcile(ws, params)["passed"] is False


# --- checks: attack surface ------------------------------------------------------------

def test_attack_surface_covers_the_spec(tmp_path):
    ws = _ws(tmp_path)
    rel = "deliverables/m1/attack-surface.json"
    surface = T.parse_openapi(ws, spec_path="inputs/openapi.json")
    _write(ws, rel, json.dumps(surface))
    assert C.attack_surface_covers_spec(ws, {"path": rel})["passed"] is True

    _write(ws, rel, json.dumps({"endpoints": [f"{e['method']} {e['path']}" for e in surface["endpoints"]],
                                "unauthenticated_routes": ["GET /health"]}))
    res = C.attack_surface_covers_spec(ws, {"path": rel})
    assert res["passed"] is False and "unauthenticated but not flagged: POST /login" in res["details"]

    (ws / "inputs" / "openapi.json").unlink()
    _write(ws, rel, "{}")
    res = C.attack_surface_covers_spec(ws, {"path": rel})
    assert res["passed"] is True and "for the human to review" in res["details"]


# --- manifest ----------------------------------------------------------------------------

def _milestone(spec, mid):
    return next(m for m in spec.manifest.milestones if m.id == mid)


def test_placeholder_patterns_allow_security_evidence(tmp_path):
    spec = load_specialist("appsec-review")
    params = next(a.params for a in _milestone(spec, "m2-findings").acceptance if a.check == "no_placeholders")
    ws = _ws(tmp_path)
    doc = _write(ws, params["path"], "\n".join([
        "Exploit: submit `{{7*7}}` as the template name; the response renders 49 (Jinja2 SSTI).",
        "```python", "# TODO: add auth check", "def export(): ...", "```", ""]))
    assert no_placeholders(ws, params, CheckContext()).passed is True
    doc.write_text(doc.read_text(encoding="utf-8") + "- TODO: write the remediation\n", encoding="utf-8")
    assert no_placeholders(ws, params, CheckContext()).passed is False


def test_milestone_estimates_fit_the_run_limits():
    spec = load_specialist("appsec-review")
    m = spec.manifest
    for ms in m.milestones:
        hi = ms.hours[1]
        assert hi * 60 <= m.limits.max_wall_minutes, ms.id
        assert hi * m.estimate.usd_per_hour <= m.limits.max_usd, ms.id


def test_osv_intake_answers_must_be_well_formed():
    spec = load_specialist("appsec-review")
    base = {"repo_provided": True, "authorized": True, "scope_file": "inputs/scope.json"}
    blocking = {m.field for m in spec.validate_intake({**base, "osv_lookup": "no",
                                                       "internal_packages": "acme-*"}) if m.blocking}
    assert blocking == {"osv_lookup", "internal_packages"}
    blocking = {m.field for m in spec.validate_intake({**base, "osv_lookup": False,
                                                       "internal_packages": ["acme-*"]}) if m.blocking}
    assert blocking == set()
