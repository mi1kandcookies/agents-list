"""
tests/specialists/test_appsec_review_e2e.py - offline end-to-end runs of the
appsec-review specialist.

ScriptedAdapter plays the model and drives the real kit loop, policy gate,
ledger, acceptance checks and evidence hashing, and the real domain tools
(secret scanner, OSV audit, OpenAPI parser, CVSS calculator, scope guard,
SARIF writer) on a tmp workspace seeded from evals/fixtures through the eval
cases' fixture maps. Some scripted turns read earlier tool results from the
conversation (the scan hits, the OSV answer, the CVSS scores), so the
deliverables are built from what the tools really returned. OSV answers come
from a fake transport; nothing touches the network. The manifest allowlists
no programs, so there is no git repo to build: the specialist never runs a
command and never changes repo/.

Secret-shaped strings are assembled at runtime so nothing secret-looking is
committed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from agentkit.errors import AgentKitError
from agentkit.evals import case_brief, load_cases, prepare_workspace
from agentkit.evidence import evidence_hash, platform_evidence
from agentkit.events import MemorySink
from agentkit.llm import ScriptedAdapter
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.types import Message, ModelResponse, Submission, ToolCall, Usage

SLUG = "appsec-review"
M1, M2, M3 = "m1-scope-threat-model", "m2-findings", "m3-report"
M1_DIR, M2_DIR, M3_DIR = (f"deliverables/{m}" for m in (M1, M2, M3))
SARIF = f"{M2_DIR}/findings.sarif"
FINDINGS_MD = f"{M2_DIR}/findings.md"
SECRETS_JSON = f"{M2_DIR}/secrets.json"
DEPENDENCIES_JSON = f"{M2_DIR}/dependencies.json"
M2_DELIVERABLES = [SARIF, FINDINGS_MD, SECRETS_JSON, DEPENDENCIES_JSON]
OSV_URL = "https://api.osv.dev/v1/query"
ADVISORY_ID = "PYSEC-2099-0001"          # synthetic advisory id (well-formed, fictional)
ADVISORY_VECTOR = "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N"
SQLI_VECTOR = "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H"
SECRET_VECTOR = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"


def _aws_key() -> str:
    return "AKIA" + ("EXAMPLEKEY" + "ABCDEF1234")[:16]


def _password() -> str:
    return "s3cr3t" + "Value" + "12345"


# --- fixtures ----------------------------------------------------------------------------

@pytest.fixture
def spec():
    return load_specialist(SLUG)


@pytest.fixture
def cases(spec):
    return {c.milestone: c for c in load_cases(spec)}


def _workspace(spec, case, tmp_path: Path) -> Path:
    """The eval case's fixtures (inputs/scope.json, inputs/openapi.json,
    repo/requirements.txt, repo/transactions.py) plus a config file with
    planted credentials."""
    ws = tmp_path / "ws"
    prepare_workspace(spec, case, ws)
    (ws / "repo" / "config.py").write_text("\n".join([
        "import os",
        "",
        'AWS_ACCESS_KEY_ID = "' + _aws_key() + '"',
        'db_password = "' + _password() + '"',
        "SESSION_SECRET = os.environ['SESSION_SECRET']",
        "",
    ]), encoding="utf-8")
    return ws


class FakeOSV:
    """OSV query API: one advisory for flask (by default), nothing for other packages."""

    def __init__(self, vulnerable=("flask",)):
        self.calls: list[tuple[str, str, str]] = []
        self.vulnerable = set(vulnerable)

    def __call__(self, method, url, headers, body, timeout):
        query = json.loads(body) if body else {}
        name = (query.get("package") or {}).get("name", "")
        self.calls.append((method, url, name))
        if url != OSV_URL:
            return 404, {}, b"not found"
        vulns = []
        if name in self.vulnerable:
            vulns = [{"id": ADVISORY_ID, "summary": "Session cookie handling flaw in flask 2.0.1",
                      "aliases": ["CVE-2099-0001"],
                      "severity": [{"type": "CVSS_V3", "score": ADVISORY_VECTOR}],
                      "affected": [{"package": {"name": "flask", "ecosystem": "PyPI"},
                                    "ranges": [{"type": "ECOSYSTEM",
                                                "events": [{"introduced": "0"}, {"fixed": "2.2.5"}]}]}]}]
        return 200, {"Content-Type": "application/json"}, json.dumps({"vulns": vulns}).encode()


# --- scripted model helpers ------------------------------------------------------------

_ids = iter(range(1, 10_000))


def _turn(*calls: tuple[str, dict]) -> ModelResponse:
    return ModelResponse(text="", stop_reason="tool_use", usage=Usage(input_tokens=100, output_tokens=50),
                         model="scripted",
                         tool_calls=[ToolCall(id=f"call_{next(_ids)}", name=n, arguments=dict(a))
                                     for n, a in calls])


def _say(text: str) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=[], stop_reason="end",
                         usage=Usage(input_tokens=100, output_tokens=50), model="scripted")


def _output(messages: list[Message], name: str, index: int = -1):
    """The JSON a tool returned earlier in the run, unwrapped from its
    <untrusted> block (domain tool output is untrusted by default)."""
    results = [r for m in messages for r in m.tool_results if r.name == name and not r.is_error]
    assert results, f"no successful {name} result in the history"
    body = re.sub(r"^<untrusted[^>]*>\n|\n</untrusted>$", "", results[index].content)
    return json.loads(body)


def _run(spec, ws, brief, milestone, steps, *, grader=None, transport=None, events=None):
    adapter = ScriptedAdapter(steps)
    ctx = RunContext(brief=brief, workspace=ws, adapter=adapter, grader=grader,
                     events=events or MemorySink(), transport=transport or FakeOSV())
    return spec.run_milestone(ctx, milestone), adapter


def _line_of(ws: Path, rel: str, needle: str) -> int:
    lines = (ws / rel).read_text(encoding="utf-8").splitlines()
    return next(i for i, line in enumerate(lines, 1) if needle in line)


def _assert_ready(sub: Submission, spec, ws: Path, milestone: str, deliverables: list[str]) -> None:
    assert sub.status == "ready_for_review", [(r.check, r.passed, r.details) for r in sub.check_results]
    assert all(r.passed is True for r in sub.check_results if r.kind == "automated")
    # the human gate comes from the manifest, and its sign-off stays pending
    gate = spec.manifest.human_gate
    assert sub.human_review.required is True and gate.required is True
    assert sub.human_review.reviewer_role == "customer security lead"
    assert sub.human_review.checklist == gate.checklist
    assert sub.human_review.disclaimer == gate.disclaimer
    human = [r for r in sub.check_results if r.check == "human_signoff"]
    assert human and all(r.passed is None and r.kind == "human" for r in human)
    # artifacts are the deliverables, each hashed as it is on disk
    assert sorted(a.path for a in sub.artifacts) == sorted(deliverables)
    for a in sub.artifacts:
        data = (ws / a.path).read_bytes()
        assert a.sha256 == hashlib.sha256(data).hexdigest() and a.bytes == len(data)
    # the evidence hash covers the saved submission, bound to the milestone's
    # SOW index (the briefs carry no milestones, so its place in the manifest)
    assert re.fullmatch(r"0x[0-9a-f]{64}", sub.evidence_hash)
    assert sub.milestone_idx == [m.id for m in spec.manifest.milestones].index(milestone)
    assert json.loads(platform_evidence(sub))["milestone_idx"] == sub.milestone_idx
    saved = json.loads(spec.submission_path(ws, milestone).read_text(encoding="utf-8"))
    assert saved["evidence_hash"] == sub.evidence_hash == evidence_hash(Submission.from_dict(saved))


# --- M1: scope, threat model and attack-surface map --------------------------------------

THREAT_MODEL = """# Threat model: Northwind Ledger (staging)

## Scope
Signed allow-list: staging.northwind-ledger.example and its subdomains. Production
(prod.northwind-ledger.example) and third-party hosts are out of scope. Static,
white-box review of repo/ only.

## Assets
- Ledger transactions and account balances (SQLite table txns)
- Session tokens issued by POST /login
- Database path and session secret read from the environment

## Trust Boundaries
- Browser or API client to the API: every request parameter is untrusted
- API to the SQLite database: queries built in repo/transactions.py

## Attack Surface
- 5 operations in inputs/openapi.json; unauthenticated: GET /health, POST /login
- GET /accounts/{accountId}/transactions passes accountId and since to list_transactions
- GET /admin/export is authenticated but exposes bulk data

## Test Plan
- A03 Injection: trace accountId and since into the SQL string in list_transactions
- A01 Broken access control: confirm account ownership on the transactions route
- A07 Identification and authentication failures: login throttling and session handling
- A05 Security misconfiguration: hard-coded credentials in configuration files
"""


def _m1_steps(threat_model: str = THREAT_MODEL, surface: str | None = None) -> list:
    return [
        _turn(("read_file", {"path": "inputs/scope.json"}),
              ("parse_openapi", {"spec_path": "inputs/openapi.json"})),
        _turn(("check_scope", {"targets": ["https://staging.northwind-ledger.example/api"]})),
        _turn(("write_file", {"path": f"{M1_DIR}/threat-model.md", "content": threat_model})),
        # the attack-surface map is the parser's own inventory (unless a test overrides it)
        lambda messages: _turn(("write_file", {
            "path": f"{M1_DIR}/attack-surface.json",
            "content": surface if surface is not None else json.dumps(_output(messages, "parse_openapi"),
                                                                      indent=2)})),
        _turn(("submit_milestone", {"summary": "Scope confirmed; 5 endpoints mapped, 2 unauthenticated",
                                    "artifacts": [f"{M1_DIR}/threat-model.md",
                                                  f"{M1_DIR}/attack-surface.json"]})),
    ]


def test_registry_loads_the_subclass_with_its_wiring(spec):
    assert type(spec).__name__ == "AppSecReview"
    assert spec.validate() == []
    tools = spec.tool_registry()
    for name in ("scan_secrets", "audit_dependencies", "parse_openapi", "cvss_base_score",
                 "check_scope", "build_sarif"):
        assert name in tools.names()
    assert tools.get("audit_dependencies").risk == "network"
    assert tools.get("scan_secrets").untrusted_output is True
    checks = spec.check_registry()
    for name in ("sarif_valid", "sarif_locations_exist", "findings_have_evidence",
                 "scope_respected", "secret_findings_reconcile"):
        assert name in checks


def test_m1_threat_model_ready_for_review(spec, cases, tmp_path):
    ws = _workspace(spec, cases[M1], tmp_path)
    events = MemorySink()
    sub, adapter = _run(spec, ws, case_brief(spec, cases[M1]), M1, _m1_steps(), events=events)

    _assert_ready(sub, spec, ws, M1, [f"{M1_DIR}/threat-model.md", f"{M1_DIR}/attack-surface.json"])
    results = {r.check: r for r in sub.check_results}
    assert results["rubric_grader"].passed is None and results["rubric_grader"].kind == "rubric"
    surface = json.loads((ws / M1_DIR / "attack-surface.json").read_text(encoding="utf-8"))
    assert surface["count"] == 5 and surface["unauthenticated"] == ["GET /health", "POST /login"]
    scope = next(e.data for e in events.of_type("tool_result") if e.data["name"] == "check_scope")
    assert '"all_in_scope": true' in scope["content"]
    # the model saw the domain tools, the playbook and the human gate
    first = adapter.calls[0]
    assert {"scan_secrets", "build_sarif", "check_scope"} <= set(first["tools"])
    assert "run_command" not in first["tools"]
    assert "Review method" in first["system"] and "customer security lead" in first["system"]


def test_m1_with_a_grader_scores_the_rubric(spec, cases, tmp_path):
    ids = ["scope_fidelity", "surface_completeness", "trust_boundaries", "test_plan_quality"]
    call = ToolCall("j1", "score_rubric", {"scores": [
        {"id": i, "score": 0.9, "rationale": "covered"} for i in ids]})
    grader = ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                           usage=Usage(), model="grader")])
    ws = _workspace(spec, cases[M1], tmp_path)
    sub, _ = _run(spec, ws, case_brief(spec, cases[M1]), M1, _m1_steps(), grader=grader)
    rubric = next(r for r in sub.check_results if r.check == "rubric_grader")
    assert rubric.passed is True and rubric.score == pytest.approx(0.9)
    assert sub.status == "ready_for_review"


def test_m1_scope_and_surface_are_recomputed(spec, cases, tmp_path):
    """An out-of-scope host planned as a target, a sibling customer host and
    an empty attack-surface map all fail; naming the out-of-scope host under
    the Scope heading (as THREAT_MODEL does) and a third-party reference do not."""
    threat_model = THREAT_MODEL + (
        "- A01: replay the IDOR probe against https://prod.northwind-ledger.example/api/accounts\n"
        "- Compare with billing.northwind-ledger.example and the guidance at https://owasp.org/Top10/\n")
    ws = _workspace(spec, cases[M1], tmp_path)
    sub, _ = _run(spec, ws, case_brief(spec, cases[M1]), M1, _m1_steps(threat_model, surface="{}"))
    results = {r.check: r for r in sub.check_results}
    scope = results["scope_respected"]
    assert scope.passed is False
    assert "prod.northwind-ledger.example (listed out of scope)" in scope.details
    assert "billing.northwind-ledger.example (a customer host outside the signed scope)" in scope.details
    assert "owasp.org" in scope.details and "owasp.org (" not in scope.details   # listed, not a violation
    surface = results["attack_surface_covers_spec"]
    assert surface.passed is False and "GET /health" in surface.details
    assert sub.status == "needs_revision"


def test_m1_missing_section_needs_revision(spec, cases, tmp_path):
    ws = _workspace(spec, cases[M1], tmp_path)
    no_plan = THREAT_MODEL.split("## Test Plan")[0]
    sub, _ = _run(spec, ws, case_brief(spec, cases[M1]), M1, _m1_steps(no_plan))
    sections = next(r for r in sub.check_results if r.check == "markdown_sections")
    assert sections.passed is False and "Test Plan" in sections.details
    assert sub.status == "needs_revision"
    assert sub.human_review.required is True


# --- M2: validated findings with SARIF evidence ------------------------------------------

def _findings(messages: list[Message], ws: Path) -> list[dict]:
    """Finding objects built from what the tools returned in this run."""
    scan = _output(messages, "scan_secrets")
    audit = _output(messages, "audit_dependencies")
    sqli_score = _output(messages, "cvss_base_score", 0)["base_score"]
    secret_score = _output(messages, "cvss_base_score", 1)["base_score"]
    aws = next(f for f in scan["findings"] if f["rule_id"] == "SECRET.AWS_ACCESS_KEY_ID")
    vuln = audit["vulnerabilities"][0]
    return [
        {"rule_id": "APPSEC.SQLI", "name": "SQL injection in list_transactions", "severity": "high",
         "message": "accountId and since are concatenated into the SQL string.",
         "file": "transactions.py",
         "start_line": _line_of(ws, "repo/transactions.py", "SELECT * FROM txns WHERE account = '"),
         "snippet": "query = \"SELECT * FROM txns WHERE account = '\" + account_id",
         "cwe": "CWE-89", "owasp": "A03", "cvss": SQLI_VECTOR, "cvss_score": sqli_score,
         "exploit": "An accountId of x' OR '1'='1 returns every account's transactions.",
         "remediation": "Bind both values as parameters, as account_balance already does."},
        # a workspace path straight from scan_secrets; build_sarif makes it repo-relative
        {"rule_id": aws["rule_id"], "name": aws["name"], "severity": aws["severity"],
         "message": f"Hard-coded AWS access key id ({aws['excerpt']}).",
         "file": aws["file"], "start_line": aws["line"], "start_column": aws["column"],
         "cwe": aws["cwe"], "owasp": "A07", "cvss": SECRET_VECTOR, "cvss_score": secret_score,
         "exploit": "Anyone with read access to the repository can call AWS as this key.",
         "remediation": "Revoke the key, load it from the environment and purge it from history."},
        {"rule_id": "APPSEC.VULNERABLE_DEPENDENCY", "name": f"Vulnerable {vuln['package']}",
         "severity": vuln["severity"].lower(),
         "message": f"{vuln['package']} {vuln['version']} is affected by {vuln['id']}.",
         "file": "requirements.txt", "start_line": _line_of(ws, "repo/requirements.txt", "flask=="),
         "snippet": f"{vuln['package']}=={vuln['version']}",
         "cwe": "CWE-1395", "owasp": "A06", "cvss": vuln["cvss"][0]["score"],
         "exploit": f"{vuln['summary']} (OSV {vuln['id']}).",
         "remediation": f"Upgrade {vuln['package']} to {vuln['fixed']} or later."},
    ]


FINDINGS_REPORT = """# Findings register

| # | Finding | Location | Severity |
|---|---------|----------|----------|
| 1 | SQL injection (CWE-89) | repo/transactions.py | High |
| 2 | Hard-coded AWS access key id (CWE-798) | repo/config.py | High |
| 3 | Vulnerable flask 2.0.1 (OSV advisory) | repo/requirements.txt | Medium |

Each finding carries its exploit scenario and fix in findings.sarif. Secret values
are redacted; only the location and the rule that fired are reported.
"""


def _m2_steps(ws: Path, findings_fn=_findings, *, extra=(), fake_secrets=(), secrets_fn=None,
              report: str = FINDINGS_REPORT) -> list:
    def write_evidence(messages):
        if secrets_fn is not None:
            secrets = secrets_fn(_output(messages, "scan_secrets")["findings"])
        else:
            secrets = _output(messages, "scan_secrets")["findings"] + list(fake_secrets)
        return _turn(("write_file", {"path": SECRETS_JSON, "content": json.dumps(secrets, indent=2)}),
                     ("build_sarif", {"findings": findings_fn(messages, ws)}))

    def cite_advisory(messages):
        vuln = _output(messages, "audit_dependencies")["vulnerabilities"][0]
        return _turn(("record_claim", {"text": f"flask 2.0.1 is affected by {vuln['id']}",
                                       "source": vuln["source"], "quote": f'"id": "{vuln["id"]}"'}))

    return [
        _turn(("scan_secrets", {"root": "repo"}),
              ("audit_dependencies", {"manifest": "repo/requirements.txt"})),
        cite_advisory,
        _turn(("cvss_base_score", {"vector": SQLI_VECTOR}),
              ("cvss_base_score", {"vector": SECRET_VECTOR})),
        *extra,
        write_evidence,
        _turn(("write_file", {"path": FINDINGS_MD, "content": report})),
        _turn(("submit_milestone", {"summary": "3 findings with SARIF evidence",
                                    "artifacts": [SARIF, FINDINGS_MD, SECRETS_JSON]})),
    ]


def _m2(spec, cases, tmp_path, **kw):
    ws = _workspace(spec, cases[M2], tmp_path)
    transport, events = FakeOSV(), MemorySink()
    sub, _ = _run(spec, ws, case_brief(spec, cases[M2]), M2, _m2_steps(ws, **kw),
                  transport=transport, events=events)
    return sub, ws, transport, events


def test_m2_findings_ready_for_review(spec, cases, tmp_path):
    sub, ws, transport, events = _m2(spec, cases, tmp_path)

    _assert_ready(sub, spec, ws, M2, M2_DELIVERABLES)
    results = {r.check: r for r in sub.check_results}
    assert ("all 3 location(s) verified: 2 by a matching source snippet, 1 by a fresh secret scan"
            in results["sarif_locations_exist"].details)
    assert results["secret_findings_reconcile"].passed is True
    assert results["cvss_consistent"].passed is True and results["no_secret_values"].passed is True
    assert "1 advisory id(s)" in results["advisories_reconcile"].details
    # the scanner caught both planted credentials, including the db_password assignment
    secrets = json.loads((ws / SECRETS_JSON).read_text(encoding="utf-8"))
    assert {(f["rule_id"], f["line"]) for f in secrets if f["file"] == "repo/config.py"} == {
        ("SECRET.AWS_ACCESS_KEY_ID", 3), ("SECRET.GENERIC_ASSIGNMENT", 4)}
    tool_errors = [e.data for e in events.of_type("tool_result") if e.data["is_error"]]
    assert not tool_errors, tool_errors

    # the SARIF log is built from the tools' answers
    log = json.loads((ws / SARIF).read_text(encoding="utf-8"))
    by_rule = {r["ruleId"]: r for r in log["runs"][0]["results"]}
    assert by_rule["APPSEC.SQLI"]["properties"]["security-severity"] == "8.8"
    assert by_rule["APPSEC.VULNERABLE_DEPENDENCY"]["level"] == "warning"       # 5.9 from the OSV vector
    rules = {r["id"]: r for r in log["runs"][0]["tool"]["driver"]["rules"]}
    assert rules["APPSEC.SQLI"]["properties"]["security-severity"] == "8.8"
    assert "security" in rules["APPSEC.SQLI"]["properties"]["tags"]
    secret_uri = by_rule["SECRET.AWS_ACCESS_KEY_ID"]["locations"][0]["physicalLocation"]["artifactLocation"]
    assert secret_uri["uri"] == "config.py"
    assert ADVISORY_ID in by_rule["APPSEC.VULNERABLE_DEPENDENCY"]["properties"]["exploit"]

    # OSV was the only host contacted, once per pinned package, and its answer
    # became a ledger source the advisory claim quotes
    assert {(m, u) for m, u, _ in transport.calls} == {("POST", OSV_URL)}
    assert len(transport.calls) == 5
    ledger = json.loads((ws / ".agentkit" / "ledger.json").read_text(encoding="utf-8"))
    assert [s["kind"] for s in ledger["sources"]] == ["tool"]
    assert ledger["claims"][0]["source"] == ledger["sources"][0]["id"]
    assert sorted(ledger["authored"]) == sorted(M2_DELIVERABLES)
    audit = json.loads((ws / DEPENDENCIES_JSON).read_text(encoding="utf-8"))
    assert audit["package_count"] == 5 and len(audit["packages"]) == 5
    assert audit["vulnerabilities"][0]["fixed"] == "2.2.5"

    # the planted credentials never leave repo/: not in deliverables, journal or submission
    for secret in (_aws_key(), _password()):
        for path in [*(ws / "deliverables").rglob("*"), *(ws / ".agentkit").rglob("*")]:
            if path.is_file():
                assert secret not in path.read_text(encoding="utf-8", errors="replace"), path


def test_m2_forged_evidence_needs_revision(spec, cases, tmp_path):
    """The agent plants the code it reports in repo/ and claims a secret the
    scanner never found: both are rejected."""
    planted = "def export(fmt):\n    return db.execute('SELECT * FROM exports WHERE fmt = ' + fmt)\n"

    def forged(messages, ws):
        findings = _findings(messages, ws)
        findings.append({"rule_id": "APPSEC.SQLI", "name": "SQL injection in export",
                         "severity": "high", "message": "fmt is concatenated into SQL.",
                         "file": "legacy_export.py", "start_line": 2,
                         "snippet": "SELECT * FROM exports WHERE fmt = ",
                         "cwe": "CWE-89", "owasp": "A03", "cvss": SQLI_VECTOR, "exploit": "fmt=' OR 1=1",
                         "remediation": "Bind fmt as a parameter."})
        return findings

    plant = _turn(("write_file", {"path": "repo/legacy_export.py", "content": planted}))
    fake = {"file": "repo/config.py", "line": 40, "rule_id": "SECRET.PRIVATE_KEY_BLOCK"}
    sub, _, _, _ = _m2(spec, cases, tmp_path, findings_fn=forged, extra=[plant], fake_secrets=[fake])

    results = {r.check: r for r in sub.check_results}
    assert results["sarif_locations_exist"].passed is False
    assert "legacy_export.py: written by the agent" in results["sarif_locations_exist"].details
    assert results["secret_findings_reconcile"].passed is False
    assert "repo/config.py:40" in results["secret_findings_reconcile"].details
    assert results["sarif_valid"].passed is True and results["findings_have_evidence"].passed is True
    assert sub.status == "needs_revision"
    assert sub.human_review.required is True


def test_m2_tampered_sarif_fails_on_recheck(spec, cases, tmp_path):
    sub, ws, _, _ = _m2(spec, cases, tmp_path)
    assert sub.status == "ready_for_review"
    log = json.loads((ws / SARIF).read_text(encoding="utf-8"))
    region = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    region["startLine"] += 3                                  # evidence no longer at that line
    del log["runs"][0]["results"][1]["properties"]["remediation"]
    (ws / SARIF).write_text(json.dumps(log), encoding="utf-8")

    results = {r.check: r for r in spec.check(ws, M2)}
    assert results["sarif_locations_exist"].passed is False
    assert "snippet does not match source" in results["sarif_locations_exist"].details
    assert results["findings_have_evidence"].passed is False
    assert results["sarif_valid"].passed is True


def test_m2_tampered_score_fails_on_recheck(spec, cases, tmp_path):
    sub, ws, _, _ = _m2(spec, cases, tmp_path)
    assert sub.status == "ready_for_review"
    log = json.loads((ws / SARIF).read_text(encoding="utf-8"))
    dep = next(r for r in log["runs"][0]["results"] if r["ruleId"] == "APPSEC.VULNERABLE_DEPENDENCY")
    dep["level"], dep["properties"]["security-severity"] = "error", "9.8"    # inflated by hand
    (ws / SARIF).write_text(json.dumps(log), encoding="utf-8")
    result = next(r for r in spec.check(ws, M2) if r.check == "cvss_consistent")
    assert result.passed is False
    assert "level 'error' should be 'warning'" in result.details and "should be 5.9" in result.details


def test_m2_invented_advisory_needs_revision(spec, cases, tmp_path):
    report = FINDINGS_REPORT + "\nrequests 2.25.1 is also affected by CVE-2099-99999 (remote code execution).\n"
    sub, _, _, _ = _m2(spec, cases, tmp_path, report=report)
    result = next(r for r in sub.check_results if r.check == "advisories_reconcile")
    assert result.passed is False and "CVE-2099-99999" in result.details
    assert ADVISORY_ID not in result.details                     # the real OSV answer reconciles
    assert sub.status == "needs_revision"


def test_m2_unreported_secret_needs_revision_until_dismissed(spec, cases, tmp_path):
    def only_aws(hits):
        return [h for h in hits if h["rule_id"] == "SECRET.AWS_ACCESS_KEY_ID"]

    sub, _, _, _ = _m2(spec, cases, tmp_path / "a", secrets_fn=only_aws)
    result = next(r for r in sub.check_results if r.check == "secret_findings_reconcile")
    assert result.passed is False
    assert "neither reported nor dismissed" in result.details and "repo/config.py:4" in result.details
    assert sub.status == "needs_revision"

    def dismiss_password(hits):
        other = [dict(h, reason="value is the documented local-development password") for h in hits
                 if h not in only_aws(hits)]
        return {"findings": only_aws(hits), "dismissed": other}

    sub, _, _, _ = _m2(spec, cases, tmp_path / "b", secrets_fn=dismiss_password)
    result = next(r for r in sub.check_results if r.check == "secret_findings_reconcile")
    assert result.passed is True, result.details
    assert "for the human reviewer to confirm" in result.details
    assert "documented local-development password" in result.details
    assert sub.status == "ready_for_review"


def test_m2_secret_value_in_register_needs_revision(spec, cases, tmp_path):
    report = FINDINGS_REPORT + f"\nThe database password is {_password()}.\n"
    sub, _, _, _ = _m2(spec, cases, tmp_path, report=report)
    result = next(r for r in sub.check_results if r.check == "no_secret_values")
    assert result.passed is False and FINDINGS_MD in result.details
    assert _password() not in result.details                     # the check names, never quotes
    assert sub.status == "needs_revision"


EMPTY_REGISTER = """# Findings register

## No findings
No weakness met the evidence bar in the reviewed scope; the secret scan and the
OSV audit came back clean. Coverage limits are stated in the final report.
"""


def test_m2_honest_empty_register_is_ready(spec, cases, tmp_path):
    """A clean result passes without inventing a finding, as long as the
    register says so."""
    ws = _workspace(spec, cases[M2], tmp_path)
    (ws / "repo" / "config.py").unlink()
    steps = [
        _turn(("scan_secrets", {"root": "repo"}),
              ("audit_dependencies", {"manifest": "repo/requirements.txt"})),
        _turn(("write_file", {"path": SECRETS_JSON, "content": '{"findings": [], "dismissed": []}'}),
              ("build_sarif", {"findings": []}),
              ("write_file", {"path": FINDINGS_MD, "content": EMPTY_REGISTER})),
        _turn(("submit_milestone", {"summary": "No findings", "artifacts": [SARIF, FINDINGS_MD, SECRETS_JSON]})),
    ]
    sub, _ = _run(spec, ws, case_brief(spec, cases[M2]), M2, steps, transport=FakeOSV(vulnerable=()))
    _assert_ready(sub, spec, ws, M2, M2_DELIVERABLES)
    results = {r.check: r for r in sub.check_results}
    assert "states so" in results["findings_have_evidence"].details

    # the same empty log without the statement is not accepted
    (ws / FINDINGS_MD).write_text("# Findings register\n\nSee the SARIF log.\n", encoding="utf-8")
    result = next(r for r in spec.check(ws, M2) if r.check == "findings_have_evidence")
    assert result.passed is False and "does not state" in result.details


# --- M3: remediation guidance and final report -------------------------------------------

def _report(disclaimer: str) -> str:
    return f"""# Northwind Ledger application security review

## Executive Summary
Three findings: two high (SQL injection, a hard-coded cloud credential) and one
medium (a vulnerable web framework version). Fix the injection and rotate the key first.

## Scope
Static review of repo/ and the staging API under the signed allow-list
(staging.northwind-ledger.example). Production was out of scope.

## Methodology
Threat model from inputs/openapi.json and source, secret scan, OSV dependency audit,
manual source-to-sink tracing, CVSS v3.1 scoring.

## Findings
1. SQL injection in list_transactions (repo/transactions.py), CVSS 8.8.
2. Hard-coded AWS access key id (repo/config.py), CVSS 7.5.
3. flask 2.0.1 affected by {ADVISORY_ID} (repo/requirements.txt).

## Remediation
See remediation.md: parameterise the query, rotate and remove the key, upgrade flask.

## Coverage Limits
No live testing; business-logic flaws outside the traced flows may remain.

{disclaimer}
"""


REMEDIATION = """# Remediation

1. repo/transactions.py list_transactions: pass account_id and since as bound
   parameters. Retest: an accountId of x' OR '1'='1 returns no rows.
2. repo/config.py: revoke the AWS key, read it from the environment, purge history.
3. repo/requirements.txt: upgrade flask to 2.2.5 or later and re-run the audit.
"""


def _m3_steps(report: str) -> list:
    return [
        _turn(("write_file", {"path": f"{M3_DIR}/remediation.md", "content": REMEDIATION}),
              ("write_file", {"path": f"{M3_DIR}/report.md", "content": report})),
        _turn(("submit_milestone", {"summary": "Final report and remediation guidance",
                                    "artifacts": [f"{M3_DIR}/report.md", f"{M3_DIR}/remediation.md"]})),
    ]


def _m3(spec, cases, tmp_path, report: str):
    """M3 on the workspace M2 left behind: its OSV answers are in the ledger,
    so the advisory the report names reconciles."""
    sub, ws, _, _ = _m2(spec, cases, tmp_path)
    assert sub.status == "ready_for_review"
    sub, adapter = _run(spec, ws, case_brief(spec, cases[M3]), M3, _m3_steps(report))
    return sub, adapter, ws


def test_m3_report_ready_for_review(spec, cases, tmp_path):
    disclaimer = spec.manifest.human_gate.disclaimer
    sub, adapter, ws = _m3(spec, cases, tmp_path, _report(disclaimer))

    _assert_ready(sub, spec, ws, M3, [f"{M3_DIR}/report.md", f"{M3_DIR}/remediation.md"])
    results = {r.check: r for r in sub.check_results}
    assert results["disclaimer_present"].passed is True
    assert results["rubric_grader"].passed is None
    assert results["advisories_reconcile"].passed is True and results["scope_respected"].passed is True
    # the kit told the model which disclaimer goes where
    assert f"Include this disclaimer verbatim in {M3_DIR}/report.md" in adapter.calls[0]["system"]
    assert disclaimer in adapter.calls[0]["system"]


def test_m3_paraphrased_disclaimer_needs_revision(spec, cases, tmp_path):
    paraphrase = "This automated review is not a pentest and is not proof of security."
    sub, _, _ = _m3(spec, cases, tmp_path, _report(paraphrase))
    results = {r.check: r.passed for r in sub.check_results}
    assert results["disclaimer_present"] is False
    assert sub.status == "needs_revision" and sub.human_review.required is True


def test_m3_advisory_without_an_osv_answer_needs_revision(spec, cases, tmp_path):
    """On a fresh workspace nothing was ever looked up, so the advisory the
    report names has no recorded source."""
    ws = _workspace(spec, cases[M3], tmp_path)
    report = _report(spec.manifest.human_gate.disclaimer)
    sub, _ = _run(spec, ws, case_brief(spec, cases[M3]), M3, _m3_steps(report))
    result = next(r for r in sub.check_results if r.check == "advisories_reconcile")
    assert result.passed is False and ADVISORY_ID in result.details
    assert sub.status == "needs_revision"


# --- policy and authorization ------------------------------------------------------------

def test_policy_keeps_the_review_in_bounds(spec, cases, tmp_path):
    ws = _workspace(spec, cases[M1], tmp_path)
    scope_before = (ws / "inputs" / "scope.json").read_bytes()
    events, transport = MemorySink(), FakeOSV()
    steps = [
        _turn(("http_fetch", {"url": "https://prod.northwind-ledger.example/api/accounts"}),
              ("http_fetch", {"url": "https://staging.northwind-ledger.example/api/health"}),
              ("run_command", {"argv": ["git", "log"]}),
              ("write_file", {"path": "inputs/scope.json", "content": '{"hosts": ["*"]}'}),
              ("build_sarif", {"findings": [], "output_path": "inputs/findings.sarif"}),
              ("scan_secrets", {"root": ".agentkit"}),
              ("read_file", {"path": "../outside.txt"})),
        _say("stopping"), _say("stopping"),
    ]
    sub, _ = _run(spec, ws, case_brief(spec, cases[M1]), M1, steps, events=events, transport=transport)
    denied = {e.data["tool"] for e in events.of_type("policy_denied")}
    assert len(events.of_type("policy_denied")) == 6
    assert denied == {"http_fetch", "write_file", "build_sarif", "scan_secrets", "read_file"}
    # no shell for this specialist: run_command is not even registered
    shell = next(e.data for e in events.of_type("tool_result") if e.data["name"] == "run_command")
    assert shell["is_error"] and "unknown tool" in shell["content"]
    assert transport.calls == []                      # no request left the workspace
    assert (ws / "inputs" / "scope.json").read_bytes() == scope_before
    assert not (ws / "inputs" / "findings.sarif").exists()
    assert sub.status == "incomplete"


def test_customer_intake_governs_what_reaches_osv(spec, cases, tmp_path):
    audit = [_turn(("audit_dependencies", {"manifest": "repo/requirements.txt", "exclude": ["jinja2"]})),
             _say("done")]

    # internal packages from the intake are never sent, on top of the model's own exclusions
    ws = _workspace(spec, cases[M2], tmp_path / "internal")
    brief = case_brief(spec, cases[M2])
    brief.intake["internal_packages"] = ["flask", "req*"]
    transport, events = FakeOSV(), MemorySink()
    _run(spec, ws, brief, M2, list(audit), transport=transport, events=events)
    assert sorted(name for _, _, name in transport.calls) == ["cryptography", "pyyaml"]
    result = next(e.data for e in events.of_type("tool_result") if e.data["name"] == "audit_dependencies")
    assert not result["is_error"]
    assert result["content"].count("listed as internal; not sent to OSV") == 3

    # opting out denies the tool before any request
    ws = _workspace(spec, cases[M2], tmp_path / "optout")
    brief = case_brief(spec, cases[M2])
    brief.intake["osv_lookup"] = False
    transport, events = FakeOSV(), MemorySink()
    _run(spec, ws, brief, M2, list(audit), transport=transport, events=events)
    assert transport.calls == []
    denied = [e.data for e in events.of_type("policy_denied")]
    assert [d["tool"] for d in denied] == ["audit_dependencies"] and "osv_lookup" in denied[0]["reason"]


def test_run_refuses_without_authorization(spec, cases, tmp_path):
    ws = _workspace(spec, cases[M1], tmp_path)
    brief = case_brief(spec, cases[M1])
    brief.intake["authorized"] = False
    adapter = ScriptedAdapter(_m1_steps())
    with pytest.raises(AgentKitError, match="authorized"):
        spec.run_milestone(RunContext(brief=brief, workspace=ws, adapter=adapter, transport=FakeOSV()), M1)
    assert adapter.calls == []                        # refused before any model call
    assert not spec.submission_path(ws, M1).exists()


def test_intake_confirmations_must_be_true(spec, cases):
    intake = dict(case_brief(spec, cases[M1]).intake)
    assert spec.validate_intake(intake) == []
    missing = spec.validate_intake({**intake, "authorized": False, "repo_provided": "maybe"})
    assert {(m.field, m.blocking) for m in missing} == {("authorized", True), ("repo_provided", True)}
    missing = spec.validate_intake({k: v for k, v in intake.items() if k != "authorized"})
    assert [m.field for m in missing] == ["authorized"]
