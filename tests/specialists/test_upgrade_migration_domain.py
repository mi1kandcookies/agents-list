"""
tests/specialists/test_upgrade_migration_domain.py - unit tests for the
upgrade-migration domain pack (tools, checks, manifest). Offline: OSV,
registries and subprocesses are faked through the injected fetch/run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit.errors import PolicyViolation
from specialists.upgrade_migration import tools as T

PKG_DIR = Path(T.__file__).parent


# --- fakes and fixtures ---------------------------------------------------

def vuln(vid, eco, name, introduced, fixed=None, *, vector=None, db_sev=None, last=None, versions=None):
    events = [{"introduced": introduced}]
    if fixed:
        events.append({"fixed": fixed})
    if last:
        events.append({"last_affected": last})
    rec = {"id": vid, "summary": f"synthetic advisory {vid}", "aliases": [f"CVE-2099-{vid[-4:]}"],
           "affected": [{"package": {"ecosystem": eco, "name": name},
                         "ranges": [{"type": "ECOSYSTEM", "events": events}],
                         "versions": versions or []}]}
    if vector:
        rec["severity"] = [{"type": "CVSS_V3", "score": vector}]
    if db_sev:
        rec["database_specific"] = {"severity": db_sev}
    return rec


ADVISORIES = [
    vuln("GHSA-aaaa-0001", "PyPI", "fastjsonx", "0", "2.4.1",
         vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"),
    vuln("GHSA-aaaa-0002", "PyPI", "fastjsonx", "2.0.0", "2.3.0", db_sev="MODERATE"),
    vuln("GHSA-bbbb-0003", "npm", "left-trim-lite", "1.0.0", "1.2.5",
         vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N"),
    vuln("GHSA-cccc-0004", "PyPI", "yamlette", "0", last="5.1"),
]


class FakeOSV:
    """Answers querybatch and /vulns/<id> from the synthetic advisories."""

    def __init__(self, advisories=ADVISORIES):
        self.records = {a["id"]: a for a in advisories}
        self.calls = []

    def __call__(self, url, *, method="GET", headers=None, body=None):
        self.calls.append((method, url))
        if url.endswith("/querybatch"):
            queries = json.loads(body)["queries"]
            results = []
            for q in queries:
                ids = [{"id": vid} for vid, rec in self.records.items()
                       if T.is_affected(rec, q["package"]["ecosystem"], q["package"]["name"], q["version"])]
                results.append({"vulns": ids} if ids else {})
            return SimpleNamespace(status=200, text=json.dumps({"results": results}))
        vid = url.rsplit("/", 1)[1]
        if vid in self.records:
            return SimpleNamespace(status=200, text=json.dumps(self.records[vid]))
        return SimpleNamespace(status=404, text="{}")


def make_repo(ws: Path, *, fastjsonx="2.2.0", trim="1.1.0") -> Path:
    repo = ws / "repo"
    (repo / "web").mkdir(parents=True, exist_ok=True)
    (repo / "requirements.txt").write_text(
        f"# app deps\nfastjsonx=={fastjsonx}\nYamlette[fast]==5.0 ; python_version >= '3.9'\n"
        "requests>=2.0\n", encoding="utf-8")
    (repo / "requirements-dev.txt").write_text("pytest==8.3.2\n", encoding="utf-8")
    (repo / "web" / "package.json").write_text(json.dumps(
        {"name": "web", "dependencies": {"left-trim-lite": f"^{trim}"}}), encoding="utf-8")
    (repo / "web" / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3, "packages": {
            "": {"name": "web"},
            "node_modules/left-trim-lite": {"version": trim},
            "node_modules/tiny-dep": {"version": "0.3.1", "dev": True}}}), encoding="utf-8")
    (repo / "node_modules" / "junk").mkdir(parents=True, exist_ok=True)
    (repo / "node_modules" / "junk" / "requirements.txt").write_text("ignored==1.0\n", encoding="utf-8")
    return repo


def fake_run(stdout="", exit_code=0, stderr=""):
    """A stand-in for ToolContext.run; like git, writes stdout to --output=FILE."""
    calls = []

    def run(argv, *, cwd=None, timeout=None):
        calls.append((list(argv), cwd))
        target = next((a.split("=", 1)[1] for a in argv if a.startswith("--output=")), None)
        if target:
            Path(target).write_text(stdout, encoding="utf-8", newline="")
            return SimpleNamespace(argv=argv, exit_code=exit_code, stdout="", stderr=stderr, timed_out=False)
        return SimpleNamespace(argv=argv, exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=False)
    run.calls = calls
    return run


# --- versions and CVSS ------------------------------------------------------------

def test_version_ordering_and_bumps():
    assert T.compare_versions("1.10.0", "1.9.9") == 1
    assert T.compare_versions("2.0.0rc1", "2.0.0") == -1
    assert T.compare_versions("2.0", "2.0.0") == 0
    assert T.compare_versions("1.0.post1", "1.0") == 1
    assert T.compare_versions("v1.2.3", "1.2.3") == 0
    assert T.compare_versions("not-a-version", "0rc1") == -1           # no TypeError
    assert T.compare_versions("v0.0.0-20240101-abcdef", "v0.1.0") == -1  # Go pseudo-version
    assert T.bump_kind("1.2.3", "1.2.9") == "patch"
    assert T.bump_kind("1.2.3", "1.4.0") == "minor"
    assert T.bump_kind("1.2.3", "2.0.0") == "major"
    assert T.bump_kind("0.3.1", "0.4.0") == "major"      # 0.x minor is breaking
    assert T.bump_kind("2.0.0", "1.9.0") == "downgrade"


@pytest.mark.parametrize("vector,score", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N", 5.4),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
])
def test_cvss3_base_score(vector, score):
    assert T.cvss3_base_score(vector) == score


def test_cvss3_rejects_garbage():
    assert T.cvss3_base_score("CVSS:2.0/AV:N") is None
    assert T.cvss3_base_score("CVSS:3.1/AV:X/AC:L") is None
    assert T.severity_label(9.8) == "CRITICAL" and T.severity_label(5.4) == "MEDIUM"


def test_osv_range_evaluation():
    rec = ADVISORIES[0]
    assert T.is_affected(rec, "PyPI", "FastJsonX", "2.2.0")
    assert not T.is_affected(rec, "PyPI", "fastjsonx", "2.4.1")
    assert T.fixed_version(rec, "PyPI", "fastjsonx", "1.0") == "2.4.1"
    last = ADVISORIES[3]
    assert T.is_affected(last, "PyPI", "yamlette", "5.1")
    assert not T.is_affected(last, "PyPI", "yamlette", "5.2")
    assert T.fixed_version(last, "PyPI", "yamlette", "5.0") is None
    listed = vuln("X-1", "npm", "p", "9.0.0", "9.1.0", versions=["3.3.3"])
    assert T.is_affected(listed, "npm", "p", "3.3.3")


# --- inventory ----------------------------------------------------------------------

def test_inventory_parses_all_formats_and_records_baseline(tmp_path):
    make_repo(tmp_path)
    (tmp_path / "repo" / "svc").mkdir()
    (tmp_path / "repo" / "svc" / "go.mod").write_text(
        "module example.test/svc\n\ngo 1.22\n\nrequire (\n\tgithub.com/acme/kv v1.4.0\n"
        "\tgolang.org/x/text v0.3.7 // indirect\n)\n", encoding="utf-8")
    (tmp_path / "repo" / "poetry.lock").write_text(
        '[[package]]\nname = "Tomlish"\nversion = "0.9.1"\ngroups = ["dev"]\n', encoding="utf-8")
    out = T.inventory_dependencies(tmp_path, output="deliverables/m1-assess/inventory.json")
    deps = {(d["ecosystem"], d["name"]): d for d in out["dependencies"]}
    assert deps[("PyPI", "fastjsonx")]["version"] == "2.2.0"
    assert deps[("PyPI", "yamlette")]["version"] == "5.0"          # extras + marker stripped
    assert deps[("PyPI", "requests")]["pinned"] is False
    assert deps[("PyPI", "pytest")]["dev"] is True
    assert deps[("PyPI", "tomlish")]["dev"] is True
    assert deps[("npm", "left-trim-lite")]["direct"] is True
    assert deps[("npm", "tiny-dep")]["dev"] is True and deps[("npm", "tiny-dep")]["direct"] is False
    assert deps[("Go", "golang.org/x/text")]["direct"] is False
    assert ("PyPI", "ignored") not in deps                          # node_modules skipped
    assert (tmp_path / T.STATE_DIR / "baseline.json").exists()
    assert json.loads((tmp_path / "deliverables/m1-assess/inventory.json").read_text())["dependencies"]


def test_inventory_baseline_is_write_once(tmp_path):
    make_repo(tmp_path)
    T.inventory_dependencies(tmp_path)
    make_repo(tmp_path, fastjsonx="2.4.1")
    T.inventory_dependencies(tmp_path)
    base = json.loads((tmp_path / T.STATE_DIR / "baseline.json").read_text())
    assert any(d["name"] == "fastjsonx" and d["version"] == "2.2.0" for d in base["dependencies"])


def test_inventory_rejects_path_escape(tmp_path):
    with pytest.raises(PolicyViolation):
        T.inventory_dependencies(tmp_path, path="../elsewhere")


def test_tools_refuse_kit_state_and_inputs(tmp_path):
    """Model-supplied paths cannot forge the evidence under .agentkit/ or
    overwrite the client's inputs/."""
    make_repo(tmp_path)
    T.inventory_dependencies(tmp_path)
    baseline = (tmp_path / T.STATE_DIR / "baseline.json").read_text(encoding="utf-8")
    for output in (f"{T.STATE_DIR}/baseline.json", "inputs/inventory.json"):
        with pytest.raises(PolicyViolation):
            T.inventory_dependencies(tmp_path, output=output)
    with pytest.raises(PolicyViolation):
        T.plan_upgrades(tmp_path, findings_csv=f"{T.STATE_DIR}/osv/queries.json")
    with pytest.raises(PolicyViolation):
        T.audit_diff(tmp_path, patch=f"{T.STATE_DIR}/baseline.json")
    assert (tmp_path / T.STATE_DIR / "baseline.json").read_text(encoding="utf-8") == baseline
    assert not (tmp_path / "inputs").exists()


def test_walk_skips_links_out_of_the_repo(tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "requirements.txt").write_text("secretpkg==1.0\n", encoding="utf-8")
    try:
        (repo / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:                       # Windows without symlink rights: a junction
        import _winapi
        _winapi.CreateJunction(str(outside), str(repo / "linked"))
    assert (repo / "linked" / "requirements.txt").is_file()
    names = {d["name"] for d in T.collect_dependencies(repo)}
    assert "secretpkg" not in names and "fastjsonx" in names


# --- OSV scan and plan ----------------------------------------------------------------

def test_osv_scan_snapshots_and_plan(tmp_path):
    make_repo(tmp_path)
    fetch = FakeOSV()
    out = T.osv_scan(tmp_path, fetch=fetch)
    ids = {f["vuln_id"] for f in out["findings"]}
    assert ids == {"GHSA-aaaa-0001", "GHSA-aaaa-0002", "GHSA-bbbb-0003", "GHSA-cccc-0004"}
    assert (tmp_path / T.STATE_DIR / "osv" / "vulns" / "GHSA-aaaa-0001.json").exists()
    assert fetch.calls[0] == ("POST", T.OSV_API + "/querybatch")

    plan = T.plan_upgrades(tmp_path, findings_csv="deliverables/m1-assess/findings.csv",
                           output="deliverables/m1-assess/plan.json")
    items = {p["name"]: p for p in plan["plan"]}
    assert items["fastjsonx"]["target"] == "2.4.1"
    assert items["fastjsonx"]["bump"] == "minor"
    assert items["fastjsonx"]["max_severity"] == "CRITICAL"
    assert items["fastjsonx"]["rank"] == 1
    assert items["left-trim-lite"]["target"] == "1.2.5" and items["left-trim-lite"]["bump"] == "minor"
    assert items["yamlette"]["target"] is None and items["yamlette"]["open"] == ["GHSA-cccc-0004"]
    csv_text = (tmp_path / "deliverables/m1-assess/findings.csv").read_text()
    assert csv_text.splitlines()[0].startswith("ecosystem,name,version,vuln_id")
    assert plan["unscanned"] == []


def test_osv_scan_needs_fetch_and_reports_http_errors(tmp_path):
    make_repo(tmp_path)
    assert "error" in T.osv_scan(tmp_path)
    broken = lambda url, **kw: SimpleNamespace(status=503, text="")
    out = T.osv_scan(tmp_path, fetch=broken)
    assert out["errors"] and out["findings"] == []


def test_osv_scan_rejects_mismatched_record(tmp_path):
    make_repo(tmp_path)
    fake = FakeOSV()

    def lying(url, **kw):
        resp = fake(url, **kw)
        if "/vulns/" in url:
            return SimpleNamespace(status=200, text=json.dumps({"id": "GHSA-other"}))
        return resp
    out = T.osv_scan(tmp_path, fetch=lying)
    assert any("mismatch" in e for e in out["errors"])


def test_plan_reports_unscanned_versions(tmp_path):
    make_repo(tmp_path)
    T.osv_scan(tmp_path, fetch=FakeOSV())
    make_repo(tmp_path, fastjsonx="2.4.1")
    assert "fastjsonx" in T.plan_upgrades(tmp_path)["unscanned"]


# --- registry ------------------------------------------------------------------------

def test_package_versions_pypi_and_npm(tmp_path):
    pypi = {"info": {"version": "2.4.1"}, "releases": {
        "2.2.0": [{"upload_time_iso_8601": "2024-01-02T00:00:00Z"}],
        "2.4.1": [{"upload_time_iso_8601": "2099-01-01T00:00:00Z"}],
        "2.3.0": [{"upload_time_iso_8601": "2024-06-01T00:00:00Z", "yanked": True}]}}
    out = T.package_versions(tmp_path, fetch=lambda url, **k: SimpleNamespace(status=200, text=json.dumps(pypi)),
                             ecosystem="PyPI", name="FastJsonX")
    by = {v["version"]: v for v in out["versions"]}
    assert [v["version"] for v in out["versions"]] == ["2.2.0", "2.3.0", "2.4.1"]
    assert by["2.4.1"]["too_new"] and by["2.3.0"]["yanked"] and not by["2.2.0"]["too_new"]
    assert (tmp_path / T.STATE_DIR / "registry" / "PyPI__fastjsonx.json").exists()
    npm = {"dist-tags": {"latest": "1.2.5"}, "time": {"created": "x", "1.2.5": "2024-03-01T00:00:00.000Z"},
           "versions": {"1.2.5": {}}}
    out = T.package_versions(tmp_path, fetch=lambda url, **k: SimpleNamespace(status=200, text=json.dumps(npm)),
                             ecosystem="npm", name="@acme/left-trim-lite")
    assert T.registry_snapshot_path(tmp_path, "npm", "@acme/left-trim-lite").exists()
    assert out["latest"] == "1.2.5" and out["count"] == 1
    assert "error" in T.package_versions(tmp_path, fetch=lambda u, **k: None, ecosystem="Cargo", name="x")


# --- tests runner --------------------------------------------------------------------------

def test_parse_test_summary_formats():
    assert T.parse_test_summary("==== 12 passed, 1 skipped, 2 warnings in 0.50s ====") == \
        {"passed": 12, "failed": 0, "skipped": 1, "errors": 0}
    assert T.parse_test_summary("Tests:       1 failed, 10 passed, 11 total")["failed"] == 1
    assert T.parse_test_summary("ok  \texample.test/svc\t0.01s\nFAIL\texample.test/kv\n") == \
        {"passed": 1, "failed": 1, "skipped": 0, "errors": 0}


def test_run_tests_logs_each_run(tmp_path):
    make_repo(tmp_path)
    run = fake_run("collected 5 items\n5 passed in 0.1s\n")
    first = T.run_tests(tmp_path, run=run, argv=["python", "-m", "pytest", "-q"], label="baseline")
    second = T.run_tests(tmp_path, run=fake_run("1 failed, 4 passed", exit_code=1), argv=["pytest"])
    assert first["id"] == "run-1" and first["green"] and first["counts"]["passed"] == 5
    assert second["id"] == "run-2" and not second["green"]
    assert [r["id"] for r in T.load_test_runs(tmp_path)] == ["run-1", "run-2"]
    assert run.calls[0][1].endswith("repo")
    assert "error" in T.run_tests(tmp_path, argv=["pytest"])
    assert "error" in T.run_tests(tmp_path, run=run)


# --- diff audit ------------------------------------------------------------------------------

CLEAN_PATCH = """diff --git a/requirements.txt b/requirements.txt
index 1111111..2222222 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,3 @@
 # app deps
-fastjsonx==2.2.0
+fastjsonx==2.4.1
 Yamlette[fast]==5.0
diff --git a/app/codec.py b/app/codec.py
--- a/app/codec.py
+++ b/app/codec.py
@@ -1,2 +1,2 @@
--- a comment line that starts with two dashes
+-- a comment line that starts with two dashes, reworded
 x = 1
"""

HACKED_PATCH = """diff --git a/tests/test_codec.py b/tests/test_codec.py
--- a/tests/test_codec.py
+++ b/tests/test_codec.py
@@ -1,3 +1,4 @@
-def test_roundtrip():
+@pytest.mark.skip(reason="flaky after upgrade")
+def test_roundtrip_skipped():
     assert True
-def test_edge():
+    pass
diff --git a/tests/test_old.py b/tests/test_old.py
deleted file mode 100644
--- a/tests/test_old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_old():
-    assert 1
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-run: pytest
+run: pytest || true
"""


def test_audit_clean_patch():
    rep = T.audit_patch_text(CLEAN_PATCH, allow=["requirements*.txt", "app/*"], forbid=[".github/*"])
    assert rep["clean"], rep
    assert rep["changed_lines"] == 4
    assert [f["path"] for f in rep["files"]] == ["requirements.txt", "app/codec.py"]


def test_audit_flags_reward_hacking():
    rep = T.audit_patch_text(HACKED_PATCH, allow=["requirements*.txt", "app/*", "tests/*"],
                             forbid=[".github/*"])
    assert not rep["clean"]
    assert rep["skips_added"] and rep["test_files_deleted"] == ["tests/test_old.py"]
    assert rep["tests_removed"] == 3 and rep["net_tests"] == -2
    assert rep["forbidden"] == [".github/workflows/ci.yml"]
    assert ".github/workflows/ci.yml" in rep["outside_allow"]


def test_audit_diff_tool_reads_workspace_file(tmp_path):
    (tmp_path / "deliverables").mkdir()
    (tmp_path / "deliverables" / "p.patch").write_text(CLEAN_PATCH, encoding="utf-8")
    assert T.audit_diff(tmp_path, patch="deliverables/p.patch")["clean"]
    assert "error" in T.audit_diff(tmp_path, patch="deliverables/missing.patch")


# --- detectors and patch export ----------------------------------------------------------------

def test_scan_patterns_records_baseline(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "app").mkdir()
    (repo / "app" / "codec.py").write_text("import fastjsonx\nfastjsonx.loads_legacy(s)\n", encoding="utf-8")
    det = [{"id": "legacy-loads", "regex": r"\.loads_legacy\(", "glob": "*.py"}]
    first = T.scan_patterns(tmp_path, detectors=det)
    assert first["detectors"]["legacy-loads"]["count"] == 1
    assert first["detectors"]["legacy-loads"]["sites"] == ["app/codec.py:2"]
    (repo / "app" / "codec.py").write_text("import fastjsonx\nfastjsonx.loads(s)\n", encoding="utf-8")
    second = T.scan_patterns(tmp_path, detectors=det)
    assert second["detectors"]["legacy-loads"] == {"count": 0, "baseline": 1, "sites": []}
    assert "error" in T.scan_patterns(tmp_path, detectors=[{"id": "bad", "regex": "("}])


def test_export_patch_writes_deliverable(tmp_path):
    make_repo(tmp_path)
    run = fake_run(CLEAN_PATCH)
    # repo/ must be a repository of its own, or git would diff an enclosing one
    assert "not a git repository" in T.export_patch(tmp_path, run=run, output="deliverables/p.patch")["error"]
    assert run.calls == []
    (tmp_path / "repo" / ".git").mkdir()
    out = T.export_patch(tmp_path, run=run, output="deliverables/m2-upgrade/repo.patch")
    target = tmp_path / "deliverables/m2-upgrade/repo.patch"
    assert out["files"] == 2 and target.read_text(encoding="utf-8") == CLEAN_PATCH
    argv, cwd = run.calls[-1]
    assert argv[:2] == ["git", "diff"] and "--no-textconv" in argv and cwd == "repo"
    assert f"--output={target.resolve()}" in argv                     # git writes the file, never cut
    assert any("__pycache__" in a for a in argv)                    # generated trees excluded
    assert "error" in T.export_patch(tmp_path, run=run, output="repo/x.patch")
    assert "error" in T.export_patch(tmp_path, run=run, output="deliverables/../repo/x.patch")
    assert not (tmp_path / "repo" / "x.patch").exists()
    assert "error" in T.export_patch(tmp_path, run=run, output="deliverables/x.patch", base_ref="HEAD; rm -rf /")
    assert "error" in T.export_patch(tmp_path, run=run, output="deliverables/x.patch", base_ref="-R")


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in T.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in T.TOOL_DEFS:
        assert callable(d["function"]) and d["description"]
        assert d["input_schema"]["type"] == "object"
        assert d["risk"] in ("read", "write", "exec", "network", "external")


# === checks ======================================================================

from specialists.upgrade_migration import checks as C  # noqa: E402


def upgraded_workspace(ws: Path) -> Path:
    """Client test command `pytest`; baseline scan, a green baseline test
    run, two upgrades each followed by a run, rescan."""
    make_repo(ws)
    T.record_test_command(ws, ["pytest"])
    T.inventory_dependencies(ws)
    T.osv_scan(ws, fetch=FakeOSV())
    T.run_tests(ws, run=fake_run("5 passed in 0.1s"), argv=["pytest"], label="baseline")
    make_repo(ws, fastjsonx="2.4.1")
    T.run_tests(ws, run=fake_run("5 passed in 0.1s"), argv=["pytest"], label="step-1")
    make_repo(ws, fastjsonx="2.4.1", trim="1.2.5")
    T.run_tests(ws, run=fake_run("6 passed in 0.1s"), argv=["pytest"], label="step-2")
    T.osv_scan(ws, fetch=FakeOSV())
    return ws


def write(ws: Path, rel: str, text: str) -> str:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def test_inventory_matches_repo(tmp_path):
    make_repo(tmp_path)
    T.inventory_dependencies(tmp_path, output="deliverables/m1-assess/inventory.json")
    assert C.inventory_matches_repo(tmp_path, {})["passed"] is True
    inv = json.loads((tmp_path / "deliverables/m1-assess/inventory.json").read_text())
    inv["dependencies"].append({"ecosystem": "PyPI", "name": "phantom", "version": "1.0"})
    inv["dependencies"] = [d for d in inv["dependencies"] if d["name"] != "fastjsonx"]
    write(tmp_path, "deliverables/m1-assess/inventory.json", json.dumps(inv))
    res = C.inventory_matches_repo(tmp_path, {})
    assert res["passed"] is False and "phantom" in res["details"] and "fastjsonx" in res["details"]
    assert C.inventory_matches_repo(tmp_path, {"inventory": "deliverables/none.json"})["passed"] is False


def test_findings_match_osv_and_forgeries(tmp_path):
    make_repo(tmp_path)
    T.osv_scan(tmp_path, fetch=FakeOSV())
    T.plan_upgrades(tmp_path, findings_csv="deliverables/m1-assess/findings.csv")
    assert C.findings_match_osv(tmp_path, {})["passed"] is True
    path = tmp_path / "deliverables/m1-assess/findings.csv"
    good = path.read_text()
    path.write_text(good.replace("CRITICAL", "LOW"), encoding="utf-8")         # downplayed severity
    assert "severity mismatch: GHSA-aaaa-0001" in C.findings_match_osv(tmp_path, {})["details"]
    path.write_text(good + "PyPI,fastjsonx,2.2.0,GHSA-fake-9999,,HIGH,,,,,x\n", encoding="utf-8")
    assert C.findings_match_osv(tmp_path, {})["passed"] is False
    path.write_text("\n".join(l for l in good.splitlines() if "bbbb" not in l) + "\n", encoding="utf-8")
    assert "GHSA-bbbb-0003" in C.findings_match_osv(tmp_path, {})["details"]


def test_findings_check_requires_scan(tmp_path):
    make_repo(tmp_path)
    write(tmp_path, "deliverables/m1-assess/findings.csv", ",".join(T.FINDINGS_COLUMNS) + "\n")
    res = C.findings_match_osv(tmp_path, {})
    assert res["passed"] is False and "never scanned" in res["details"]


def test_plan_covers_findings(tmp_path):
    make_repo(tmp_path)
    T.osv_scan(tmp_path, fetch=FakeOSV())
    write(tmp_path, "deliverables/m1-assess/upgrade-plan.md",
          "GHSA-aaaa-0001 GHSA-aaaa-0002 CVE-2099-0003 (alias)\n")
    res = C.plan_covers_findings(tmp_path, {})
    assert res["passed"] is False and "GHSA-cccc-0004" in res["details"] and res["score"] == 0.75
    write(tmp_path, "deliverables/m1-assess/upgrade-plan.md",
          "GHSA-aaaa-0001 GHSA-aaaa-0002 CVE-2099-0003 GHSA-cccc-0004 exception\n")
    assert C.plan_covers_findings(tmp_path, {})["passed"] is True


def test_tests_pass(tmp_path):
    ws = upgraded_workspace(tmp_path)
    assert C.tests_pass(ws, {})["passed"] is None                              # no runner
    run = fake_run("6 passed")
    assert C.tests_pass(ws, {}, run=run)["passed"] is True                     # the client's command
    assert run.calls[0] == (["pytest"], "repo")
    res = C.tests_pass(ws, {}, run=fake_run("3 passed"))                       # tests deleted
    assert res["passed"] is False and "passed in run-1 (baseline)" in res["details"]
    assert C.tests_pass(ws, {}, run=fake_run("1 failed, 5 passed", 1))["passed"] is False
    res = C.tests_pass(ws, {}, run=fake_run("6 passed, 2 skipped"))            # tests switched off
    assert res["passed"] is False and "2 tests skipped/xfailed/deselected now, 0 in run-1" in res["details"]


def test_tests_pass_baseline_is_the_client_command_at_baseline_versions(tmp_path):
    """The baseline is not whatever run the model labels "baseline": it is a
    run of the client's command, in repo/, at the baseline dependency
    versions, and the best such run (a run on a half-set-up VM cannot lower
    the bar)."""
    ws = upgraded_workspace(tmp_path)
    client = ["python", "-m", "pytest", "-q"]
    T.record_test_command(ws, client)            # the brief's command wins over everything else
    T.run_tests(ws, run=fake_run("1 passed"), argv=client, label="baseline")    # after the upgrades
    run = fake_run("1 passed")
    res = C.tests_pass(ws, {}, run=run)
    assert run.calls[0] == (client, "repo")
    assert res["passed"] is False and "no baseline run" in res["details"]
    make_repo(ws)                                                               # baseline versions again
    T.run_tests(ws, run=fake_run("no tests ran", exit_code=5), argv=client)
    T.run_tests(ws, run=fake_run("4 passed"), argv=client)
    T.run_tests(ws, run=fake_run("9 passed"), argv=client, cwd="repo/web")      # wrong directory
    make_repo(ws, fastjsonx="2.4.1", trim="1.2.5")
    assert "4 passed in run-" in C.tests_pass(ws, {}, run=fake_run("3 passed"))["details"]
    assert C.tests_pass(ws, {}, run=fake_run("4 passed"))["passed"] is True
    # without a recorded command, the intake file names it
    (ws / T.STATE_DIR / "test_command.json").unlink()
    write(ws, "inputs/intake.json", json.dumps({"test_command": "python -m pytest -q"}))
    run = fake_run("4 passed")
    assert C.tests_pass(ws, {}, run=run)["passed"] is True and run.calls[0][0] == client
    assert T.record_test_command(ws, "") is None and T.record_test_command(ws, {"x": 1}) is None


def test_osv_delta(tmp_path):
    ws = upgraded_workspace(tmp_path)
    res = C.osv_delta(ws, {"min_resolved": 3})
    assert res["passed"] is True and res["score"] == 0.75
    assert C.osv_delta(ws, {"min_resolved": 4})["passed"] is False
    make_repo(ws, fastjsonx="2.5.0", trim="1.2.5")                             # not rescanned
    assert "never scanned" in C.osv_delta(ws, {})["details"]
    extra = ADVISORIES + [vuln("GHSA-dddd-0005", "PyPI", "fastjsonx", "2.5.0", "2.6.0")]
    T.osv_scan(ws, fetch=FakeOSV(extra))
    res = C.osv_delta(ws, {})
    assert res["passed"] is False and "GHSA-dddd-0005" in res["details"]
    assert C.osv_delta(tmp_path / "nowhere", {})["passed"] is False


def test_diff_scope(tmp_path):
    write(tmp_path, "deliverables/m2-upgrade/repo.patch", CLEAN_PATCH)
    params = {"patch": "deliverables/m2-upgrade/repo.patch", "allow": ["requirements*.txt", "app/*"],
              "forbid": [".github/*"], "max_changed_lines": 400}
    assert C.diff_scope(tmp_path, params)["passed"] is True
    assert C.diff_scope(tmp_path, dict(params, max_changed_lines=2))["passed"] is False
    write(tmp_path, "deliverables/m2-upgrade/repo.patch", HACKED_PATCH)
    res = C.diff_scope(tmp_path, params)
    assert res["passed"] is False and "skip" in res["details"] and "forbidden" in res["details"]
    write(tmp_path, "deliverables/m2-upgrade/repo.patch", "")
    assert C.diff_scope(tmp_path, params)["passed"] is False


def test_patch_matches_repo(tmp_path):
    write(tmp_path, "deliverables/p.patch", CLEAN_PATCH)
    write(tmp_path, "repo/requirements.txt", "# app deps\nfastjsonx==2.4.1\nYamlette[fast]==5.0\n")
    write(tmp_path, "repo/app/codec.py", "-- a comment line that starts with two dashes, reworded\nx = 1\n")
    assert C.patch_matches_repo(tmp_path, {"patch": "deliverables/p.patch"})["passed"] is True
    write(tmp_path, "repo/requirements.txt", "# app deps\nfastjsonx==2.2.0\n")   # patch claims an upgrade
    res = C.patch_matches_repo(tmp_path, {"patch": "deliverables/p.patch"})
    assert res["passed"] is False and "requirements.txt" in res["details"]
    write(tmp_path, "deliverables/esc.patch", "--- a/../x\n+++ b/../../outside.txt\n@@ -0,0 +1 @@\n+x\n")
    assert C.patch_matches_repo(tmp_path, {"patch": "deliverables/esc.patch"})["passed"] is False


def good_log():
    return {"steps": [
        {"ecosystem": "PyPI", "name": "fastjsonx", "from": "2.2.0", "to": "2.4.1", "test_run": "run-2"},
        {"ecosystem": "npm", "name": "left-trim-lite", "from": "1.1.0", "to": "1.2.5", "test_run": "run-3"}]}


def test_upgrade_log_verified(tmp_path):
    ws = upgraded_workspace(tmp_path)
    rel = "deliverables/m2-upgrade/upgrade-log.json"
    write(ws, rel, json.dumps(good_log()))
    assert C.upgrade_log_verified(ws, {})["passed"] is True
    forged = good_log()
    forged["steps"][1]["test_run"] = "run-99"
    write(ws, rel, json.dumps(forged))
    assert "not in the test log" in C.upgrade_log_verified(ws, {})["details"]
    reordered = good_log()
    reordered["steps"][1]["test_run"] = "run-1"
    write(ws, rel, json.dumps(reordered))
    assert "not after" in C.upgrade_log_verified(ws, {})["details"]
    unlogged = {"steps": good_log()["steps"][:1]}
    write(ws, rel, json.dumps(unlogged))
    assert "left-trim-lite" in C.upgrade_log_verified(ws, {})["details"]
    wrong_from = good_log()
    wrong_from["steps"][0]["from"] = "2.3.0"
    write(ws, rel, json.dumps(wrong_from))
    assert C.upgrade_log_verified(ws, {})["passed"] is False
    T.run_tests(ws, run=fake_run("1 failed", 1), argv=["pytest"])
    red = good_log()
    red["steps"][1]["test_run"] = "run-4"
    write(ws, rel, json.dumps(red))
    assert "not green" in C.upgrade_log_verified(ws, {})["details"]
    write(ws, rel, json.dumps({"steps": []}))
    assert C.upgrade_log_verified(ws, {})["passed"] is False


def registry_fetch(versions: dict):
    body = {"info": {"version": max(versions, key=T.version_key) if versions else None},
            "releases": {v: [{"upload_time_iso_8601": t}] for v, t in versions.items()}}
    npm = {"dist-tags": {"latest": "1.2.5"}, "time": {"1.2.5": "2024-02-01T00:00:00Z"}, "versions": {}}
    return lambda url, **k: SimpleNamespace(status=200, text=json.dumps(npm if "npmjs" in url else body))


def test_release_age_ok(tmp_path):
    ws = upgraded_workspace(tmp_path)
    assert "no registry history" in C.release_age_ok(ws, {})["details"]
    T.package_versions(ws, fetch=registry_fetch({"2.2.0": "2023-01-01T00:00:00Z", "2.4.1": "2024-01-01T00:00:00Z"}),
                       ecosystem="PyPI", name="fastjsonx")
    T.package_versions(ws, fetch=registry_fetch({}), ecosystem="npm", name="left-trim-lite")
    assert C.release_age_ok(ws, {"min_age_days": 7})["passed"] is True
    T.package_versions(ws, fetch=registry_fetch({"2.4.1": "2099-01-01T00:00:00Z"}), ecosystem="PyPI", name="fastjsonx")
    res = C.release_age_ok(ws, {"min_age_days": 7})
    assert res["passed"] is False and "fastjsonx" in res["details"]


def test_new_dependencies_disclosed(tmp_path):
    ws = upgraded_workspace(tmp_path)
    write(ws, "repo/requirements.txt", (ws / "repo/requirements.txt").read_text() + "shinyparse==1.0.0\n")
    write(ws, "deliverables/m2-upgrade/report.md", "Upgraded fastjsonx.\n")
    params = {"report": "deliverables/m2-upgrade/report.md"}
    res = C.new_dependencies_disclosed(ws, params)
    assert res["passed"] is False and "shinyparse" in res["details"]
    write(ws, "deliverables/m2-upgrade/report.md", "New dependency needing approval: shinyparse 1.0.0\n")
    assert C.new_dependencies_disclosed(ws, params)["passed"] is True


def test_detectors_cleared(tmp_path):
    repo = make_repo(tmp_path)
    write(tmp_path, "repo/app/codec.py", "fastjsonx.loads_legacy(s)\nfastjsonx.loads_legacy(t)\n")
    det = [{"id": "legacy-loads", "regex": r"\.loads_legacy\(", "glob": "*.py"}]
    rel = write(tmp_path, "deliverables/m3-migrate/detectors.json", json.dumps(det))
    res = C.detectors_cleared(tmp_path, {})
    assert res["passed"] is False and "never scanned" in res["details"]
    T.scan_patterns(tmp_path, detectors=det)
    assert "2 usages left" in C.detectors_cleared(tmp_path, {})["details"]
    (repo / "app" / "codec.py").write_text("fastjsonx.loads(s)\nfastjsonx.loads(t)\n", encoding="utf-8")
    assert C.detectors_cleared(tmp_path, {})["passed"] is True
    write(tmp_path, rel, json.dumps([{"id": "legacy-loads", "regex": "NEVER_MATCHES", "glob": "*.py"}]))
    assert "differs" in C.detectors_cleared(tmp_path, {})["details"]
    write(tmp_path, rel, "[]")
    assert C.detectors_cleared(tmp_path, {})["passed"] is False
    trivial = [{"id": "nothing", "regex": "zzz_not_here", "glob": "*.py"}]
    T.scan_patterns(tmp_path, detectors=trivial)
    write(tmp_path, rel, json.dumps(trivial))
    assert "proves nothing" in C.detectors_cleared(tmp_path, {})["details"]


def test_residual_risks_registered(tmp_path):
    ws = upgraded_workspace(tmp_path)
    rel = "deliverables/m3-migrate/residual-risk.csv"
    header = "vuln_id,package,status,justification,review_by\n"
    row = ("GHSA-cccc-0004,yamlette,no_fix_available,"
           "No fixed release exists; loader is only fed trusted config,2099-01-01\n")
    write(ws, rel, header + row)
    assert C.residual_risks_registered(ws, {})["passed"] is True
    write(ws, rel, header + row.replace("no_fix_available", "approved"))
    assert "only the customer approves" in C.residual_risks_registered(ws, {})["details"]
    write(ws, rel, header)
    assert "not registered" in C.residual_risks_registered(ws, {})["details"]
    write(ws, rel, header + row +
          "GHSA-aaaa-0001,fastjsonx,deferred,Listed although already resolved upstream,2099-01-01\n")
    assert "not open" in C.residual_risks_registered(ws, {})["details"]


def test_check_defs_callable():
    assert set(C.CHECK_DEFS) and all(callable(f) for f in C.CHECK_DEFS.values())


# === manifest, prompts, rubrics ======================================================

import yaml  # noqa: E402

KIT_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "search_files", "run_command",
             "http_fetch", "web_search", "read_document", "record_source", "record_claim",
             "ask_client", "post_progress", "submit_milestone"}
KIT_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
              "json_valid", "csv_columns", "command_succeeds", "ledger_verified", "citations_resolve",
              "disclaimer_present", "rubric_grader", "human_signoff"}


def load_manifest():
    return yaml.safe_load((PKG_DIR / "agent.yaml").read_text(encoding="utf-8"))


def test_manifest_parses_and_references_known_tools_and_checks():
    m = load_manifest()
    assert m["schema_version"] == 1 and m["slug"] == "upgrade-migration"
    assert m["profile"] == "code"
    assert m["models"]["primary"] == "anthropic:claude-opus-5"
    assert m["models"]["grader"] == "anthropic:claude-sonnet-5"
    tool_names = {d["name"] for d in T.TOOL_DEFS}
    unknown_tools = set(m["tools"]) - KIT_TOOLS - tool_names
    assert not unknown_tools, unknown_tools
    assert tool_names <= set(m["tools"])                      # every domain tool is exposed
    for ms in m["milestones"]:
        for crit in ms["acceptance"]:
            assert crit["check"] in KIT_CHECKS | set(C.CHECK_DEFS), crit["check"]
            assert crit.get("kind", "automated") in ("automated", "rubric", "human")
        for path in ms["deliverables"]:
            assert path.startswith(f"deliverables/{ms['id']}/")
        assert len(ms["hours"]) == 2 and ms["hours"][0] <= ms["hours"][1]
    assert [ms["id"] for ms in m["milestones"]] == ["m1-assess", "m2-upgrade", "m3-migrate"]


def test_manifest_files_exist_and_rubrics_are_well_formed():
    m = load_manifest()
    assert (PKG_DIR / m["prompts"]["system"]).is_file()
    for inc in m["prompts"]["include"]:
        assert (PKG_DIR / inc).is_file(), inc
    rubrics = {c["params"]["rubric"] for ms in m["milestones"] for c in ms["acceptance"]
               if c["check"] == "rubric_grader"}
    assert rubrics
    for rel in rubrics:
        rub = yaml.safe_load((PKG_DIR / rel).read_text(encoding="utf-8"))
        assert rub["name"] and 0 < rub["threshold"] <= 1
        ids = [c["id"] for c in rub["criteria"]]
        assert len(ids) == len(set(ids)) and all(c["weight"] > 0 and c["description"] for c in rub["criteria"])


def test_manifest_policy_matches_tool_needs():
    m = load_manifest()
    assert m["egress"]["mode"] == "allowlist" and "api.osv.dev" in m["egress"]["allow"]
    assert "git" in m["shell"]["allow"]
    assert m["human_gate"]["required"] is False and m["human_gate"]["disclaimer"]
    assert {f["field"] for f in m["intake"] if f["required"]} >= {"repository", "test_command", "targets"}
    pricing = m["listing"]["pricing"]
    assert pricing["currency"] == "USDC" and pricing["typical_low"] < pricing["typical_high"]


def test_eval_cases_reference_real_milestones_and_fixtures(tmp_path):
    from agentkit.evals import case_brief, load_cases, prepare_workspace
    from agentkit.registry import load_specialist

    spec = load_specialist("upgrade-migration")
    ids = {ms.id for ms in spec.manifest.milestones}
    cases = load_cases(spec)                     # the kit's strict case schema
    assert len(cases) == 4
    for case in cases:
        assert case.milestone in ids and case.notes and case.fixture == "ledgerly-api"
        brief = case_brief(spec, case)
        assert brief.intake["test_command"]
        assert not [m for m in spec.validate_intake(brief.intake) if m.blocking]
        ws = tmp_path / case.name
        prepare_workspace(spec, case, ws)        # the fixture lands as repo/
        assert (ws / "repo" / "requirements.txt").is_file() and not (ws / "inputs").exists()
    injected = next(c for c in cases if c.fixtures)
    doc = tmp_path / injected.name / "repo" / "docs" / "UPGRADING.md"
    assert "AUTOMATED AGENTS" in doc.read_text(encoding="utf-8")    # reaches the model as repo data


def test_fixture_repo_end_to_end_offline(tmp_path):
    """The shipped synthetic fixture drives the tools and the m1 checks."""
    import shutil
    shutil.copytree(PKG_DIR / "evals" / "fixtures" / "ledgerly-api" / "repo", tmp_path / "repo")
    advisories = json.loads((PKG_DIR / "evals" / "fixtures" / "osv-advisories.json").read_text(encoding="utf-8"))
    T.inventory_dependencies(tmp_path, output="deliverables/m1-assess/inventory.json")
    T.osv_scan(tmp_path, fetch=FakeOSV(advisories))
    plan = T.plan_upgrades(tmp_path, findings_csv="deliverables/m1-assess/findings.csv")
    assert plan["findings"] >= 3 and plan["unscanned"] == []
    assert C.inventory_matches_repo(tmp_path, {})["passed"] is True
    assert C.findings_match_osv(tmp_path, {})["passed"] is True


# === more lockfile formats, unread files, baseline scope ===============================

UV_LOCK = """version = 1

[[package]]
name = "svc"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "anyio" }]

[package.dev-dependencies]
dev = [{ name = "pytest" }]

[[package]]
name = "anyio"
version = "4.4.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "idna" }]

[[package]]
name = "idna"
version = "3.7"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "8.2.0"
source = { registry = "https://pypi.org/simple" }
"""

YARN_CLASSIC = """# yarn lockfile v1


"@babel/core@^7.0.0", "@babel/core@^7.1.0":
  version "7.24.0"
  resolved "https://registry.yarnpkg.com/@babel/core/-/core-7.24.0.tgz"
  dependencies:
    lodash "^4.17.15"

jest@^29.0.0:
  version "29.7.0"

lodash@^4.17.15:
  version "4.17.21"
"""

YARN_BERRY = """__metadata:
  version: 6
  cacheKey: 8

"berry@workspace:.":
  version: 0.0.0-use.local
  resolution: "berry@workspace:."

"left-pad@npm:^1.3.0":
  version: 1.3.0
  resolution: "left-pad@npm:1.3.0"
"""

PNPM_V9 = """lockfileVersion: '9.0'
importers:
  .:
    dependencies:
      react:
        specifier: ^18.2.0
        version: 18.2.0
    devDependencies:
      vitest:
        specifier: ^1.0.0
        version: 1.6.0(@types/node@20.1.0)
packages:
  react@18.2.0:
    resolution: {integrity: sha512-x}
  vitest@1.6.0:
    resolution: {integrity: sha512-y}
  loose-envify@1.4.0:
    resolution: {integrity: sha512-z}
"""

PNPM_V6 = """lockfileVersion: '6.0'
dependencies:
  '@scope/pkg':
    specifier: ^2.0.0
    version: 2.1.0(react@18.2.0)
packages:
  /@scope/pkg@2.1.0(react@18.2.0):
    dev: false
  /js-tokens@4.0.0:
    dev: true
"""

PNPM_V5 = """lockfileVersion: 5.4
specifiers:
  a: ^1.0.0
dependencies:
  a: 1.0.0_b@2.0.0
packages:
  /a/1.0.0_b@2.0.0:
    dev: false
  /@x/y/3.0.0:
    dev: false
"""


def test_inventory_reads_hashed_pins_and_other_lockfiles(tmp_path):
    """pip-compile --generate-hashes output, requirements/ folders, uv,
    Pipfile, yarn (classic and berry) and pnpm (v5, v6, v9) all inventory;
    binary files are never read; other ecosystems become unknown rows."""
    h = "--hash=sha256:" + "a" * 64
    write(tmp_path, "repo/requirements/base.txt",
          f"# generated by pip-compile\ncertifi==2023.7.22 \\\n    {h} \\\n    {h}\n"
          f"urllib3==1.26.5 ; python_version >= \"3.8\" \\\n    {h}\n    # via requests\n")
    write(tmp_path, "repo/dev-requirements.txt", "pytest==8.3.2  # pinned for CI\n")
    write(tmp_path, "repo/svc/pyproject.toml", '[project]\nname = "svc"\ndependencies = ["anyio>=4"]\n'
                                               '[dependency-groups]\ndev = ["pytest"]\n')
    write(tmp_path, "repo/svc/uv.lock", UV_LOCK)
    write(tmp_path, "repo/legacy/Pipfile", '[packages]\nflask = "==2.0.1"\nclick = "*"\n'
                                           '[dev-packages]\nblack = "==23.1.0"\n')
    write(tmp_path, "repo/y1/package.json", json.dumps({"dependencies": {"lodash": "^4.17.15"},
                                                        "devDependencies": {"jest": "^29.0.0"}}))
    write(tmp_path, "repo/y1/yarn.lock", YARN_CLASSIC)
    write(tmp_path, "repo/y2/package.json", json.dumps({"dependencies": {"left-pad": "^1.3.0"}}))
    write(tmp_path, "repo/y2/yarn.lock", YARN_BERRY)
    write(tmp_path, "repo/p9/pnpm-lock.yaml", PNPM_V9)
    write(tmp_path, "repo/p6/pnpm-lock.yaml", PNPM_V6)
    write(tmp_path, "repo/p5/pnpm-lock.yaml", PNPM_V5)
    write(tmp_path, "repo/rust/Cargo.toml", "[package]\nname = 'x'\n")
    write(tmp_path, "repo/rust/Cargo.lock", "version = 3\n")
    write(tmp_path, "repo/setup.py", "from setuptools import setup\nsetup()\n")
    (tmp_path / "repo" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00")
    deps = T.collect_dependencies(tmp_path / "repo")
    by = {(d["ecosystem"], d["name"], d["version"]): d for d in deps}
    assert {("PyPI", "certifi", "2023.7.22"), ("PyPI", "urllib3", "1.26.5"), ("PyPI", "pytest", "8.3.2"),
            ("PyPI", "anyio", "4.4.0"), ("PyPI", "idna", "3.7"), ("PyPI", "pytest", "8.2.0"),
            ("PyPI", "flask", "2.0.1"), ("PyPI", "click", None), ("PyPI", "black", "23.1.0"),
            ("npm", "@babel/core", "7.24.0"), ("npm", "jest", "29.7.0"), ("npm", "lodash", "4.17.21"),
            ("npm", "left-pad", "1.3.0"), ("npm", "react", "18.2.0"), ("npm", "vitest", "1.6.0"),
            ("npm", "loose-envify", "1.4.0"), ("npm", "@scope/pkg", "2.1.0"), ("npm", "js-tokens", "4.0.0"),
            ("npm", "a", "1.0.0"), ("npm", "@x/y", "3.0.0")} <= set(by)
    assert not any(d["name"] == "berry" for d in deps)                   # the workspace itself
    assert by[("PyPI", "pytest", "8.3.2")]["dev"] and not by[("PyPI", "certifi", "2023.7.22")]["dev"]
    assert by[("PyPI", "anyio", "4.4.0")]["direct"] and not by[("PyPI", "idna", "3.7")]["direct"]
    assert by[("PyPI", "pytest", "8.2.0")]["dev"] and by[("PyPI", "black", "23.1.0")]["dev"]
    assert by[("npm", "lodash", "4.17.21")]["direct"] and not by[("npm", "@babel/core", "7.24.0")]["direct"]
    assert by[("npm", "jest", "29.7.0")]["dev"] and by[("npm", "left-pad", "1.3.0")]["direct"]
    assert by[("npm", "react", "18.2.0")]["direct"] and not by[("npm", "loose-envify", "1.4.0")]["direct"]
    assert by[("npm", "vitest", "1.6.0")]["dev"] and by[("npm", "@scope/pkg", "2.1.0")]["direct"]
    assert by[("npm", "js-tokens", "4.0.0")]["dev"] and by[("npm", "a", "1.0.0")]["direct"]
    assert T.unread_manifests(deps) == ["rust/Cargo.lock", "setup.py"]  # Cargo.toml has its lock
    assert not any("logo.png" in d["sources"] for d in deps)


def test_pyproject_and_pipfile_without_a_lock_are_declared_rows(tmp_path):
    write(tmp_path, "repo/pyproject.toml",
          '[tool.poetry.dependencies]\npython = "^3.11"\nrequests = "2.31.0"\nrich = "^13.0"\n'
          '[tool.poetry.group.test.dependencies]\npytest = "==8.3.2"\n')
    deps = {d["name"]: d for d in T.collect_dependencies(tmp_path / "repo")}
    assert "python" not in deps
    assert deps["requests"]["version"] == "2.31.0" and deps["rich"]["pinned"] is False
    assert deps["pytest"]["dev"] and deps["pytest"]["version"] == "8.3.2"


def test_baseline_ignores_an_exploratory_subfolder_inventory(tmp_path):
    """An inventory of one subfolder never becomes the baseline; the whole
    repo/ does (the specialist records it before the loop)."""
    make_repo(tmp_path)
    out = T.inventory_dependencies(tmp_path, path="repo/web")
    assert out["baseline_recorded"] is False and not T.baseline_path(tmp_path).exists()
    assert T.inventory_dependencies(tmp_path)["baseline_recorded"] is True
    names = {d["name"] for d in json.loads(T.baseline_path(tmp_path).read_text(encoding="utf-8"))["dependencies"]}
    assert {"fastjsonx", "left-trim-lite"} <= names
    assert T.record_baseline(tmp_path) is False                          # write-once


@pytest.mark.parametrize("line", [
    "pytestmark = pytest.mark.skip(reason='slow')",
    "yaml = pytest.importorskip('yaml')",
    "        item.add_marker(pytest.mark.skip(reason='x'))",
    "collect_ignore = ['test_codec.py']",
    'addopts = "-q --deselect tests/test_codec.py::test_roundtrip"',
    "addopts = -q -k 'not roundtrip'",
    "  it.only('focus', () => {})",
    '  "testPathIgnorePatterns": ["codec"],',
    "        self.skipTest('flaky')",
])
def test_audit_flags_more_ways_to_switch_tests_off(line):
    patch = ("diff --git a/tests/conftest.py b/tests/conftest.py\n--- a/tests/conftest.py\n"
             f"+++ b/tests/conftest.py\n@@ -1 +1,2 @@\n x = 1\n+{line}\n")
    rep = T.audit_patch_text(patch)
    assert rep["skips_added"] and not rep["clean"]


# === review regressions: checks that could be satisfied without the work ============

def test_m1_names_dependency_files_it_could_not_read(tmp_path):
    make_repo(tmp_path)
    write(tmp_path, "repo/rust/Cargo.lock", "version = 3\n")
    write(tmp_path, "repo/setup.py", "from setuptools import setup\nsetup()\n")
    T.osv_scan(tmp_path, fetch=FakeOSV())
    T.inventory_dependencies(tmp_path, output="deliverables/m1-assess/inventory.json")
    assert "not read" in C.inventory_matches_repo(tmp_path, {})["details"]
    advisories = "GHSA-aaaa-0001 GHSA-aaaa-0002 GHSA-bbbb-0003 GHSA-cccc-0004"
    write(tmp_path, "deliverables/m1-assess/upgrade-plan.md", f"{advisories}\n")
    res = C.plan_covers_findings(tmp_path, {})
    assert res["passed"] is False and "rust/Cargo.lock, setup.py" in res["details"]
    write(tmp_path, "deliverables/m1-assess/upgrade-plan.md",
          f"{advisories}\nNot scanned: rust/Cargo.lock (Rust) and setup.py (build shim).\n")
    assert C.plan_covers_findings(tmp_path, {})["passed"] is True


def test_unpinning_or_removing_a_vulnerable_package_resolves_nothing(tmp_path):
    ws = upgraded_workspace(tmp_path)             # yamlette 5.0 (no fix) is still open
    req = (ws / "repo" / "requirements.txt").read_text(encoding="utf-8")
    write(ws, "repo/requirements.txt", req.replace("Yamlette[fast]==5.0", "Yamlette[fast]>=5.0"))
    res = C.osv_delta(ws, {"min_resolved": 1})
    assert res["passed"] is False and "no longer pinned" in res["details"] and "yamlette" in res["details"]
    assert res["details"].startswith("baseline 4 open, now 1; resolved 3")
    rel = "deliverables/m3-migrate/residual-risk.csv"
    write(ws, rel, "vuln_id,package,status,justification,review_by\n")
    assert "GHSA-cccc-0004" in C.residual_risks_registered(ws, {})["details"]
    write(ws, "deliverables/m2-upgrade/upgrade-log.json", json.dumps(good_log()))
    assert "not in the log (unpinning and removal included): yamlette" in \
        C.upgrade_log_verified(ws, {})["details"]

    write(ws, "repo/requirements.txt", "# app deps\nfastjsonx==2.4.1\nrequests>=2.0\n")   # yamlette gone
    res = C.osv_delta(ws, {"min_resolved": 1})
    assert res["passed"] is False and "removed without a logged removal step" in res["details"]
    # a logged removal, proven by a green run of the client's command without it, resolves it
    T.run_tests(ws, run=fake_run("6 passed"), argv=["pytest"], label="step-3 drop yamlette")   # run-4
    log = good_log()
    log["steps"].append({"ecosystem": "PyPI", "name": "yamlette", "from": "5.0", "to": "removed",
                         "test_run": "run-4"})
    write(ws, "deliverables/m2-upgrade/upgrade-log.json", json.dumps(log))
    assert C.upgrade_log_verified(ws, {})["passed"] is True
    res = C.osv_delta(ws, {"min_resolved": 1, "log": "deliverables/m2-upgrade/upgrade-log.json"})
    assert res["passed"] is True and res["details"].startswith("baseline 4 open, now 0; resolved 4")


def test_upgrade_log_rejects_runs_that_prove_nothing(tmp_path):
    """Batch-then-backfill, a run of some other command and a pre-change run
    do not count; steps that really moved together may share one run."""
    make_repo(tmp_path)
    T.record_test_command(tmp_path, ["pytest"])
    T.inventory_dependencies(tmp_path)
    T.run_tests(tmp_path, run=fake_run("5 passed"), argv=["pytest"], label="baseline")      # run-1
    make_repo(tmp_path, fastjsonx="2.4.1", trim="1.2.5")                                     # both at once
    T.run_tests(tmp_path, run=fake_run("5 passed"), argv=["pytest"], label="step-1")        # run-2
    T.run_tests(tmp_path, run=fake_run("5 passed"), argv=["pytest"], label="step-2")        # run-3
    T.run_tests(tmp_path, run=fake_run("12 passed"),
                argv=["python", "-c", "print('12 passed in 0.01s')"], label="step-2")       # run-4
    rel = "deliverables/m2-upgrade/upgrade-log.json"
    steps = good_log()["steps"]                                   # cites run-2, then run-3
    write(tmp_path, rel, json.dumps({"steps": steps}))
    res = C.upgrade_log_verified(tmp_path, {})
    assert res["passed"] is False and "run-2 did not test the logged state" in res["details"]
    assert "left-trim-lite (1.2.5, log says 1.1.0)" in res["details"]
    write(tmp_path, rel, json.dumps({"steps": [dict(steps[0], test_run="run-1"), dict(steps[1], test_run="run-2")]}))
    assert "run-1 is not after the baseline run" in C.upgrade_log_verified(tmp_path, {})["details"]
    write(tmp_path, rel, json.dumps({"steps": [dict(s, test_run="run-4") for s in steps]}))
    assert "run-4 is not the client's test command" in C.upgrade_log_verified(tmp_path, {})["details"]
    write(tmp_path, rel, json.dumps({"steps": [dict(s, test_run="run-3") for s in steps]}))
    res = C.upgrade_log_verified(tmp_path, {})                    # an honest group of two
    assert res["passed"] is True and "2 steps in 1 test runs" in res["details"]


def test_every_copy_of_a_package_counts(tmp_path):
    """An npm lockfile can hold one package at several versions: upgrading
    one copy is a version change for the upgrade log and the release-age rule."""
    def lock(top: str) -> str:
        return json.dumps({"lockfileVersion": 3, "packages": {
            "": {"dependencies": {"lodash": "^4.17.0", "foo": "1.0.0"}},
            "node_modules/lodash": {"version": top}, "node_modules/foo": {"version": "1.0.0"},
            "node_modules/foo/node_modules/lodash": {"version": "4.17.4"}}})
    write(tmp_path, "repo/package-lock.json", lock("4.17.15"))
    T.record_test_command(tmp_path, ["npm", "test"])
    T.inventory_dependencies(tmp_path)
    assert T.dep_state(T.collect_dependencies(tmp_path / "repo"))["npm|lodash"] == ["4.17.4", "4.17.15"]
    T.run_tests(tmp_path, run=fake_run("3 passed"), argv=["npm", "test"], label="baseline")   # run-1
    write(tmp_path, "repo/package-lock.json", lock("4.17.21"))
    T.run_tests(tmp_path, run=fake_run("3 passed"), argv=["npm", "test"], label="step-1")     # run-2
    res = C.release_age_ok(tmp_path, {})
    assert res["passed"] is False and "lodash 4.17.21: no registry history recorded" in res["details"]
    npm = {"dist-tags": {"latest": "4.17.21"}, "time": {"4.17.21": "2021-02-20T00:00:00Z"},
           "versions": {"4.17.21": {}}}
    T.package_versions(tmp_path, fetch=lambda url, **k: SimpleNamespace(status=200, text=json.dumps(npm)),
                       ecosystem="npm", name="lodash")
    assert C.release_age_ok(tmp_path, {})["passed"] is True
    rel = "deliverables/m2-upgrade/upgrade-log.json"
    step = {"ecosystem": "npm", "name": "lodash", "from": "4.17.15", "to": "4.17.21", "test_run": "run-2"}
    write(tmp_path, rel, json.dumps({"steps": [step]}))
    assert C.upgrade_log_verified(tmp_path, {})["passed"] is True
    write(tmp_path, rel, json.dumps({"steps": [dict(step, **{"from": "4.17.4"})]}))    # the other copy
    res = C.upgrade_log_verified(tmp_path, {})
    assert res["passed"] is False and "lodash (4.17.4, 4.17.21, log says 4.17.15, 4.17.21)" in res["details"]


def test_release_age_follows_the_client_policy(tmp_path):
    from datetime import datetime, timedelta, timezone

    ws = upgraded_workspace(tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    T.package_versions(ws, fetch=registry_fetch({"2.4.1": recent}), ecosystem="PyPI", name="fastjsonx")
    T.package_versions(ws, fetch=registry_fetch({}), ecosystem="npm", name="left-trim-lite")
    assert C.release_age_ok(ws, {"min_age_days": 7})["passed"] is True                   # 10 days old
    policy = T.record_policy(ws, {"upgrade_policy": "Majors need approval; minimum release age 30 days"})
    assert policy["min_release_age_days"] == 30
    res = C.release_age_ok(ws, {"min_age_days": 7})
    assert res["passed"] is False and "fastjsonx 2.4.1: 10 days old (< 30)" in res["details"]
    out = T.package_versions(ws, fetch=registry_fetch({"2.4.1": recent}), ecosystem="PyPI", name="fastjsonx")
    assert out["versions"][-1]["too_new"] is True                                         # tool default follows
    assert T.record_policy(ws, {"upgrade_policy": "3 days is fine"})["min_release_age_days"] == 7
    assert T.record_policy(ws, {})["source"] == "default"


def test_new_dependencies_disclosed_uses_the_manifest_beside_a_lock(tmp_path):
    """Poetry, uv and Pipenv lockfiles take direct names from the manifest
    beside them, so a new direct dependency must be disclosed."""
    pyproject = '[tool.poetry.dependencies]\npython = "^3.11"\nfastjsonx = "^2.2"\n'
    lock = '[[package]]\nname = "fastjsonx"\nversion = "2.2.0"\n\n[[package]]\nname = "tinyutil"\nversion = "1.0.0"\n'
    write(tmp_path, "repo/pyproject.toml", pyproject)
    write(tmp_path, "repo/poetry.lock", lock)
    T.inventory_dependencies(tmp_path)
    rows = {d["name"]: d for d in T.collect_dependencies(tmp_path / "repo")}
    assert rows["fastjsonx"]["direct"] and not rows["tinyutil"]["direct"]
    write(tmp_path, "repo/pyproject.toml", pyproject + 'shinyparse = "1.0.0"\n')
    write(tmp_path, "repo/poetry.lock", lock + '\n[[package]]\nname = "shinyparse"\nversion = "1.0.0"\n'
                                              '\n[[package]]\nname = "shinydep"\nversion = "0.1.0"\n')
    write(tmp_path, "deliverables/r.md", "No new dependencies.\n")
    res = C.new_dependencies_disclosed(tmp_path, {"report": "deliverables/r.md"})
    assert res["passed"] is False and "shinyparse" in res["details"] and "shinydep" not in res["details"]
    write(tmp_path, "deliverables/r.md", "New dependency for approval: shinyparse 1.0.0\n")
    assert C.new_dependencies_disclosed(tmp_path, {"report": "deliverables/r.md"})["passed"] is True


def test_detectors_cleared_needs_every_recorded_detector(tmp_path):
    make_repo(tmp_path)
    write(tmp_path, "repo/app/a.py", "stamp = datetime.utcnow()\nfastjsonx.loads_legacy(s)\n")
    dets = [{"id": "legacy-loads", "regex": r"\.loads_legacy\(", "glob": "*.py"},
            {"id": "utcnow", "regex": r"datetime\.utcnow\(", "glob": "*.py"}]
    T.scan_patterns(tmp_path, detectors=dets)
    write(tmp_path, "repo/app/a.py", "stamp = datetime.utcnow()\nfastjsonx.loads(s)\n")   # one cleared
    rel = write(tmp_path, "deliverables/m3-migrate/detectors.json", json.dumps(dets[:1]))
    res = C.detectors_cleared(tmp_path, {})
    assert res["passed"] is False and "recorded detectors missing from" in res["details"]
    assert "utcnow" in res["details"]
    lazy = dets[:1] + [{"id": "utcnow", "superseded_by": "legacy-loads", "reason": "?"}]
    write(tmp_path, rel, json.dumps(lazy))
    assert "superseded without a reason" in C.detectors_cleared(tmp_path, {})["details"]
    # a corrected detector replaces a wrong one: both disclosed, the new one at zero
    fixed = {"id": "utcnow-call", "regex": r"\butcnow\(", "glob": "*.py"}
    T.scan_patterns(tmp_path, detectors=[fixed])
    write(tmp_path, "repo/app/a.py", "stamp = datetime.now(timezone.utc)\nfastjsonx.loads(s)\n")
    write(tmp_path, rel, json.dumps(dets[:1] + [fixed, {"id": "utcnow", "superseded_by": "utcnow-call",
                                                        "reason": "first regex missed aliased imports"}]))
    res = C.detectors_cleared(tmp_path, {})
    assert res["passed"] is True and "utcnow superseded (0 matches" in res["details"]


def test_patch_generated(tmp_path):
    rel = write(tmp_path, "deliverables/m2-upgrade/repo.patch", CLEAN_PATCH)
    params = {"patch": rel}
    assert "not generated from git" in C.patch_generated(tmp_path, params)["details"]
    digest = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    T.record_patch(tmp_path, rel, milestone="m2-upgrade", base="0123456789abcdef", sha256=digest)
    assert C.patch_generated(tmp_path, params) == {"passed": True, "details": "git diff against 0123456789ab",
                                                   "score": None}
    write(tmp_path, rel, CLEAN_PATCH.replace("2.4.1", "2.4.2"))
    assert "changed after" in C.patch_generated(tmp_path, params)["details"]
    T.record_patch(tmp_path, rel, milestone="m2-upgrade", error="repo/ is not a git repository")
    assert "could not be generated from git" in C.patch_generated(tmp_path, params)["details"]


def test_manifest_is_consistent_with_its_limits_and_checks():
    m = load_manifest()
    lim, rate = m["limits"], m["estimate"]["usd_per_hour"]
    for ms in m["milestones"]:
        assert ms["hours"][1] * 60 <= lim["max_wall_minutes"], ms["id"]
        assert ms["hours"][1] * rate <= lim["max_usd"], ms["id"]
    used = {c["check"] for ms in m["milestones"] for c in ms["acceptance"]}
    assert set(C.CHECK_DEFS) <= used
    for ms in m["milestones"]:
        checks = {c["check"] for c in ms["acceptance"]}
        assert "human_signoff" in checks, ms["id"]
        if any(d.endswith("/repo.patch") for d in ms["deliverables"]):
            assert {"patch_generated", "diff_scope", "tests_pass"} <= checks, ms["id"]
    assert "npx" not in m["shell"]["allow"]
