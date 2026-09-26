"""
tests/specialists/test_upgrade_migration_domain.py - unit tests for the
upgrade-migration domain pack (tools, checks, manifest). Offline: OSV,
registries and subprocesses are faked through the injected fetch/run.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    calls = []

    def run(argv, *, cwd=None, timeout=None):
        calls.append((list(argv), cwd))
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
    with pytest.raises(ValueError):
        T.inventory_dependencies(tmp_path, path="../elsewhere")


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
    assert (tmp_path / T.STATE_DIR / "registry" / "PyPI__FastJsonX.json").exists()
    npm = {"dist-tags": {"latest": "1.2.5"}, "time": {"created": "x", "1.2.5": "2024-03-01T00:00:00.000Z"},
           "versions": {"1.2.5": {}}}
    out = T.package_versions(tmp_path, fetch=lambda url, **k: SimpleNamespace(status=200, text=json.dumps(npm)),
                             ecosystem="npm", name="left-trim-lite")
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
    out = T.export_patch(tmp_path, run=run, output="deliverables/m2-upgrade/repo.patch")
    assert out["files"] == 2 and (tmp_path / "deliverables/m2-upgrade/repo.patch").read_text() == CLEAN_PATCH
    assert run.calls[-1][0][:2] == ["git", "diff"]
    assert "error" in T.export_patch(tmp_path, run=run, output="repo/x.patch")
    assert "error" in T.export_patch(tmp_path, run=run, output="deliverables/x.patch", base_ref="HEAD; rm -rf /")


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in T.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in T.TOOL_DEFS:
        assert callable(d["function"]) and d["description"]
        assert d["input_schema"]["type"] == "object"
        assert d["risk"] in ("read", "write", "exec", "network", "external")
