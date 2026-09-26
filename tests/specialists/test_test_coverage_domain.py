"""test-coverage specialist domain pack: tools, checks and manifest wiring."""
import json
from pathlib import Path

import pytest
import yaml

from agentkit.errors import PolicyViolation, ToolError
from specialists.test_coverage import checks as C
from specialists.test_coverage import tools as T

COBERTURA_BEFORE = """<?xml version="1.0" ?>
<coverage version="7.6" line-rate="0.5" branch-rate="0.25">
  <sources><source>/work/repo</source></sources>
  <packages><package name="ledgerly"><classes>
    <class name="rates.py" filename="ledgerly/rates.py" line-rate="0.5" branch-rate="0.25">
      <lines>
        <line number="1" hits="1"/>
        <line number="2" hits="1" branch="true" condition-coverage="50% (1/2)"/>
        <line number="3" hits="0"/>
        <line number="4" hits="0" branch="true" condition-coverage="0% (0/2)"/>
      </lines>
    </class>
    <class name="util.py" filename="ledgerly/util.py" line-rate="1" branch-rate="1">
      <lines><line number="1" hits="3"/><line number="2" hits="3"/></lines>
    </class>
  </classes></package></packages>
</coverage>
"""

COBERTURA_AFTER = COBERTURA_BEFORE.replace('number="3" hits="0"', 'number="3" hits="2"').replace(
    'number="4" hits="0" branch="true" condition-coverage="0% (0/2)"',
    'number="4" hits="1" branch="true" condition-coverage="100% (2/2)"')

LCOV = """TN:
SF:src/billing/invoice.ts
DA:1,1
DA:2,0
DA:3,4
BRDA:3,0,0,1
BRDA:3,0,1,-
LF:3
LH:2
end_of_record
SF:src/billing/tax.ts
LF:10
LH:5
BRF:4
BRH:1
end_of_record
"""

JACOCO = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">
<report name="orders"><package name="com/example/orders">
  <sourcefile name="Pricing.java">
    <line nr="10" mi="0" ci="3" mb="1" cb="1"/>
    <line nr="11" mi="2" ci="0" mb="0" cb="0"/>
  </sourcefile>
</package></report>
"""

GO_PROFILE = """mode: set
example.com/fleet/route.go:10.2,12.3 2 1
example.com/fleet/route.go:14.2,15.3 1 0
example.com/fleet/route.go:10.2,12.3 2 0
"""


def junit(cases):
    body = "".join(
        f'<testcase classname="tests.test_rates" name="{n}">'
        + ("<failure message='x'/>" if o == "failed" else "<error/>" if o == "error"
           else "<skipped/>" if o == "skipped" else "")
        + "</testcase>" for n, o in cases)
    return f'<?xml version="1.0"?><testsuites><testsuite name="s">{body}</testsuite></testsuites>'


def write(ws: Path, rel: str, text: str) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


PATCH_TESTS_ONLY = """diff --git a/tests/test_rates.py b/tests/test_rates.py
new file mode 100644
--- /dev/null
+++ b/tests/test_rates.py
@@ -0,0 +1,7 @@
+from ledgerly.rates import convert
+
+def test_convert_rounds_half_even():
+    assert convert(10, 0.333) == 3.33
+
+def test_convert_zero():
+    assert convert(0, 2) == 0
"""

PATCH_TOUCHES_SRC = PATCH_TESTS_ONLY + """diff --git a/ledgerly/rates.py b/ledgerly/rates.py
--- a/ledgerly/rates.py
+++ b/ledgerly/rates.py
@@ -1,2 +1,2 @@
-def convert(a, r): return round(a * r, 2)
+def convert(a, r): return 3.33
"""


# --- tools -------------------------------------------------------------------

def test_cobertura_summary_counts_lines_and_branches(tmp_path):
    write(tmp_path, "inputs/coverage.xml", COBERTURA_BEFORE)
    out = T.parse_coverage(tmp_path, path="inputs/coverage.xml", out="deliverables/m1-baseline/cov.json")
    rates = out["files"]["ledgerly/rates.py"]
    assert (rates["lines_total"], rates["lines_covered"]) == (4, 2)
    assert (rates["branches_total"], rates["branches_covered"]) == (4, 1)
    assert rates["missing_lines"] == [3, 4]
    assert out["totals"]["line_pct"] == round(100 * 4 / 6, 2)
    assert json.loads((tmp_path / "deliverables/m1-baseline/cov.json").read_text())["format"] == "cobertura"


def test_lcov_jacoco_and_go_formats(tmp_path):
    lcov = T.summarize_coverage(LCOV)
    assert lcov["format"] == "lcov"
    assert lcov["files"]["src/billing/invoice.ts"]["lines_covered"] == 2
    assert lcov["files"]["src/billing/invoice.ts"]["branches_total"] == 2
    assert lcov["files"]["src/billing/tax.ts"]["branches_covered"] == 1  # summary-only record
    jac = T.summarize_coverage(JACOCO)
    assert jac["format"] == "jacoco"
    assert jac["files"]["com/example/orders/Pricing.java"]["missing_lines"] == [11]
    go = T.summarize_coverage(GO_PROFILE)
    # duplicate block keeps the max hit count, so the first block stays covered
    assert go["files"]["example.com/fleet/route.go"]["lines_covered"] == 2
    assert go["totals"]["lines_total"] == 3


def test_coverage_include_filter_and_bad_input(tmp_path):
    only = T.summarize_coverage(COBERTURA_BEFORE, include=["ledgerly/util.py"])
    assert list(only["files"]) == ["ledgerly/util.py"]
    with pytest.raises(ToolError):
        T.summarize_coverage("hello world")
    with pytest.raises(ToolError):
        T.summarize_coverage('<!DOCTYPE x [<!ENTITY a "b">]><coverage/>')


def test_paths_are_jailed(tmp_path):
    # Direct calls apply the kit's PolicyGate path rules, like a run's resolve_path.
    with pytest.raises(PolicyViolation):
        T.parse_coverage(tmp_path, path="../outside.xml")
    write(tmp_path, "inputs/coverage.xml", COBERTURA_BEFORE)
    with pytest.raises(PolicyViolation):
        T.parse_coverage(tmp_path, path="inputs/coverage.xml", out="inputs/summary.json")
    with pytest.raises(PolicyViolation):
        T.parse_coverage(tmp_path, path="inputs/coverage.xml", out=".agentkit/x.json")
    write(tmp_path, ".agentkit/journal.xml", COBERTURA_BEFORE)
    with pytest.raises(PolicyViolation):  # the kit's own files are not readable either
        T.parse_coverage(tmp_path, path=".agentkit/journal.xml")
    write(tmp_path, ".agentkit/run-01.xml", junit([("a", "passed")]))
    write(tmp_path, "deliverables/run-02.xml", junit([("a", "passed")]))
    # nor matched by a glob
    assert T.parse_test_results(tmp_path, paths="*/run-*.xml")["files"] == ["deliverables/run-02.xml"]
    with pytest.raises(PolicyViolation):
        T.parse_test_results(tmp_path, paths="//evil-host/share/*.xml")


def test_coverage_delta_scoped(tmp_path):
    write(tmp_path, "b.xml", COBERTURA_BEFORE)
    write(tmp_path, "a.xml", COBERTURA_AFTER)
    d = T.coverage_delta(tmp_path, before="b.xml", after="a.xml", scope=["ledgerly/rates.py"])
    assert d["line_pct_before"] == 50.0 and d["line_pct_after"] == 100.0
    assert d["line_delta_pp"] == 50.0
    assert d["branch_delta_pp"] == 50.0
    assert d["lines_total_before"] == d["lines_total_after"] == 4


def test_glob_semantics():
    assert T.path_matches("test_x.py", ["**/test_*.py"])
    assert T.path_matches("pkg/a/test_x.py", ["**/test_*.py"])
    assert not T.path_matches("src/test_utils/app.py", ["**/test_*.py"])
    assert T.path_matches("tests/deep/x.json", ["tests/**"])
    assert T.path_matches("ledgerly/rates.py", ["ledgerly"])
    assert not T.path_matches("ledgerly2/rates.py", ["ledgerly"])


def test_junit_census_flaky_broken(tmp_path):
    write(tmp_path, "runs/run-01.xml", junit([("a", "passed"), ("b", "failed"), ("c", "skipped")]))
    write(tmp_path, "runs/run-02.xml", junit([("a", "failed"), ("b", "error"), ("c", "skipped")]))
    census = T.parse_test_results(tmp_path, paths="runs/run-*.xml", out="deliverables/census.json")
    assert census["flaky"] == ["tests.test_rates::a"]
    assert census["broken"] == ["tests.test_rates::b"]
    assert census["skipped"] == ["tests.test_rates::c"]
    assert census["all_green"] is False
    with pytest.raises(ToolError):
        T.parse_test_results(tmp_path, paths="nothing/*.xml")


def test_run_test_matrix_uses_injected_runner(tmp_path):
    (tmp_path / "repo").mkdir()
    calls = []

    def fake_run(argv, *, cwd=None, timeout=None):
        calls.append(argv)
        junit_path = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
        seed = int(next(a for a in argv if a.startswith("-p")).split("=")[1])
        Path(junit_path).write_text(junit([("a", "passed"), ("b", "failed" if seed == 1002 else "passed")]))
        return {"exit_code": 1 if seed == 1002 else 0, "stdout": "", "stderr": "", "timed_out": False}

    out = T.run_test_matrix(tmp_path, run=fake_run, runs=3, runs_dir="deliverables/m1-baseline/runs",
                            argv=["pytest", "--junitxml={junit}", "-prandomly-seed={seed}"])
    assert len(calls) == 3 and calls[0][2] == "-prandomly-seed=1001"
    assert out["flaky"] == ["tests.test_rates::b"]
    assert (tmp_path / "deliverables/m1-baseline/runs/matrix.json").is_file()
    with pytest.raises(ToolError):
        T.run_test_matrix(tmp_path, runs=2, runs_dir="r", argv=["pytest"])  # no runner
    with pytest.raises(ToolError):
        T.run_test_matrix(tmp_path, run=fake_run, runs=99, runs_dir="r", argv=["pytest"])


def test_diff_scope_flags_production_changes_and_renames(tmp_path):
    write(tmp_path, "ok.patch", PATCH_TESTS_ONLY)
    write(tmp_path, "bad.patch", PATCH_TOUCHES_SRC)
    ok = T.diff_scope(tmp_path, patch="ok.patch")
    assert ok["test_only"] and ok["lines_added"] == 7 and ok["files"][0]["status"] == "added"
    bad = T.diff_scope(tmp_path, patch="bad.patch")
    assert bad["outside_test_paths"] == ["ledgerly/rates.py"] and not bad["test_only"]
    rename = ("diff --git a/ledgerly/old.py b/tests/old.py\nsimilarity index 100%\n"
              "rename from ledgerly/old.py\nrename to tests/old.py\n")
    write(tmp_path, "mv.patch", rename)
    assert T.diff_scope(tmp_path, patch="mv.patch")["outside_test_paths"] == ["ledgerly/old.py"]
    assert T.diff_scope(tmp_path, patch="bad.patch", allow=["ledgerly/rates.py"])["test_only"]


def git_writes(argv, text):
    """What git does with --output=<file>: the file gets the text, stdout nothing."""
    target = next(a.split("=", 1)[1] for a in argv if a.startswith("--output="))
    Path(target).write_text(text, encoding="utf-8")
    return {"exit_code": 0, "stdout": "", "stderr": ""}


def test_export_patch_writes_git_diff(tmp_path):
    (tmp_path / "repo").mkdir()
    seen = []
    # Longer than the kit's cap on captured command output: git must write
    # the file itself, or the middle of the patch would be dropped.
    big = PATCH_TESTS_ONLY + "".join(PATCH_TESTS_ONLY.replace("test_rates", f"test_r{i}") for i in range(200))

    def fake_run(argv, *, cwd=None, timeout=None):
        seen.append(argv)
        return git_writes(argv, big) if "diff" in argv else {"exit_code": 0, "stdout": "", "stderr": ""}

    out = T.export_patch(tmp_path, run=fake_run, out="deliverables/m3-coverage-uplift/repo.patch")
    target = tmp_path / "deliverables/m3-coverage-uplift/repo.patch"
    assert out["test_only"] and out["files_changed"] == 201 and seen[0][:3] == ["git", "add", "-N"]
    assert f"--output={target.resolve()}" in seen[1] and len(big) > 30_000
    assert target.read_text() == big
    with pytest.raises(ToolError):
        T.export_patch(tmp_path, run=fake_run, out="x.patch", base="--output=/etc/x")


def test_assertion_free_python_and_other_languages(tmp_path):
    src = ("import pytest\n"
           "def test_real():\n    assert 1 + 1 == 2\n"
           "def test_nothing():\n    x = 1\n"
           "def test_trivial():\n    assert True\n"
           "def test_self_compare():\n    v = 3\n    assert v == v\n"
           "def test_raises():\n    with pytest.raises(ValueError):\n        int('x')\n"
           "class TestX:\n    def test_method(self):\n        self.assertEqual(1, 1)\n")
    write(tmp_path, "repo/tests/test_mix.py", src)
    out = T.find_assertion_free_tests(tmp_path, paths=["repo/tests/test_mix.py"])
    names = {f["test"]: f["reason"] for f in out["assertion_free"]}
    assert names == {"test_nothing": "no assertion", "test_trivial": "only trivially-true asserts",
                     "test_self_compare": "only trivially-true asserts"}
    js = "describe('x', () => {\n  it('adds', () => { expect(add(1,2)).toBe(3) })\n  it('runs', () => { add(1,2) })\n})\n"
    go = ("package fleet\nimport \"testing\"\nfunc TestA(t *testing.T) {\n if f() != 1 { t.Errorf(\"bad\") }\n}\n"
          "func TestB(t *testing.T) {\n f()\n}\n")
    java = "class PT {\n @Test\n void prices() { assertEquals(1, p()); }\n @Test\n void runs() { p(); }\n}\n"
    assert [f["test"] for f in T.assertion_free_in_source("a.test.ts", js)] == ["runs"]
    assert [f["test"] for f in T.assertion_free_in_source("route_test.go", go)] == ["TestB"]
    assert [f["test"] for f in T.assertion_free_in_source("PricingTest.java", java)] == ["runs"]


def test_assertion_free_patch_mode_only_checks_added_tests(tmp_path):
    write(tmp_path, "repo/tests/test_old.py",
          "def test_legacy():\n    pass\n\ndef test_new_one():\n    total = 2\n")
    patch = ("diff --git a/tests/test_old.py b/tests/test_old.py\n--- a/tests/test_old.py\n"
             "+++ b/tests/test_old.py\n@@ -1,2 +1,5 @@\n def test_legacy():\n     pass\n+\n"
             "+def test_new_one():\n+    total = 2\n")
    write(tmp_path, "p.patch", patch)
    out = T.find_assertion_free_tests(tmp_path, patch="p.patch")
    assert [f["test"] for f in out["assertion_free"]] == ["test_new_one"]
    write(tmp_path, "ok.patch", PATCH_TESTS_ONLY)
    assert T.find_assertion_free_tests(tmp_path, patch="ok.patch")["clean"]


MUTATION = {"schemaVersion": "1", "files": {
    "ledgerly/rates.py": {"language": "python", "mutants": [
        {"id": "1", "mutatorName": "ArithmeticOperator", "status": "Killed"},
        {"id": "2", "mutatorName": "ConditionalExpression", "status": "Survived",
         "location": {"start": {"line": 4, "column": 5}}},
        {"id": "3", "mutatorName": "StringLiteral", "status": "Timeout"},
        {"id": "4", "mutatorName": "BooleanLiteral", "status": "CompileError"}]},
    "ledgerly/util.py": {"language": "python", "mutants": [
        {"id": "5", "mutatorName": "ArithmeticOperator", "status": "NoCoverage"}]}}}


def test_mutation_report_score_and_survivors(tmp_path):
    write(tmp_path, "mut.json", json.dumps(MUTATION))
    out = T.parse_mutation_report(tmp_path, path="mut.json")
    assert out["detected"] == 2 and out["undetected"] == 2 and out["score_pct"] == 50.0
    scoped = T.parse_mutation_report(tmp_path, path="mut.json", scope=["ledgerly/rates.py"])
    assert scoped["score_pct"] == round(200 / 3, 2)
    assert scoped["survivors"][0]["line"] == 4
    write(tmp_path, "bad.json", "[1,2]")
    with pytest.raises(ToolError):
        T.parse_mutation_report(tmp_path, path="bad.json")


def test_rank_targets_orders_by_risk(tmp_path):
    write(tmp_path, "cov.xml", COBERTURA_BEFORE)
    write(tmp_path, "churn.csv", "path,commits\nledgerly/rates.py,12\nledgerly/util.py,40\n")
    out = T.rank_targets(tmp_path, coverage="cov.xml", churn="churn.csv", out="deliverables/t.csv")
    assert [r["path"] for r in out["targets"]] == ["ledgerly/rates.py", "ledgerly/util.py"]
    assert out["targets"][1]["risk_score"] == 0.0  # fully covered
    assert out["targets"][0]["proposed_floor_pct"] == 70
    lines = (tmp_path / "deliverables/t.csv").read_text().splitlines()
    assert lines[0] == ",".join(T.TARGET_COLUMNS)
    write(tmp_path, "badchurn.csv", "file,n\n")
    with pytest.raises(ToolError):
        T.rank_targets(tmp_path, coverage="cov.xml", churn="badchurn.csv")


def test_git_churn_counts_commits(tmp_path):
    (tmp_path / "repo").mkdir()

    def fake_run(argv, *, cwd=None, timeout=None):
        return git_writes(argv, "a.py\nb.py\n\na.py\n")

    out = T.git_churn(tmp_path, run=fake_run, out="deliverables/churn.csv")
    assert out["top"][0] == ("a.py", 2)
    assert (tmp_path / "deliverables/churn.csv").read_text().startswith("path,commits\na.py,2")


def test_secret_scan_flags_real_looking_values_only(tmp_path):
    key = "AKIA" + "Q" * 16
    token = "gh" + "p_" + "a1B2" * 9
    patch = ("diff --git a/tests/test_s.py b/tests/test_s.py\n--- /dev/null\n+++ b/tests/test_s.py\n"
             "@@ -0,0 +1,4 @@\n"
             f"+AWS = '{key}'\n+TOKEN = '{token}'\n+api_key = 'dummy-key-for-tests-only'\n"
             "+password = 'Zq8vLr2mTx9wPk4n'\n")
    write(tmp_path, "s.patch", patch)
    out = T.scan_patch_secrets(tmp_path, patch="s.patch")
    assert [h["kind"] for h in out["findings"]] == ["aws_access_key_id", "github_token", "assigned_secret"]
    assert key not in json.dumps(out)
    write(tmp_path, "ok.patch", PATCH_TESTS_ONLY)
    assert T.scan_patch_secrets(tmp_path, patch="ok.patch")["clean"]


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in T.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in T.TOOL_DEFS:
        assert callable(d["function"]) and d["risk"] in ("read", "write", "exec", "network")
        assert d["input_schema"]["type"] == "object" and d["description"]


# --- checks ------------------------------------------------------------------


def test_coverage_summary_matches_and_forgery_fails(tmp_path):
    write(tmp_path, "deliverables/m1-baseline/coverage.xml", COBERTURA_BEFORE)
    T.parse_coverage(tmp_path, path="deliverables/m1-baseline/coverage.xml",
                     out="deliverables/m1-baseline/coverage-summary.json")
    params = {"report": "deliverables/m1-baseline/coverage.xml",
              "summary": "deliverables/m1-baseline/coverage-summary.json"}
    assert C.coverage_summary_matches(tmp_path, params)["passed"] is True
    forged = json.loads((tmp_path / params["summary"]).read_text())
    forged["totals"]["line_pct"] = 91.5
    forged["files"]["ledgerly/rates.py"]["lines_covered"] = 4
    (tmp_path / params["summary"]).write_text(json.dumps(forged))
    res = C.coverage_summary_matches(tmp_path, params)
    assert res["passed"] is False and "line_pct" in res["details"]
    assert C.coverage_summary_matches(tmp_path, {"report": "nope.xml", "summary": "x"})["passed"] is False


def test_coverage_delta_min_pass_fail_and_shrinking_denominator(tmp_path):
    write(tmp_path, "b.xml", COBERTURA_BEFORE)
    write(tmp_path, "a.xml", COBERTURA_AFTER)
    ok = C.coverage_delta_min(tmp_path, {"before": "b.xml", "after": "a.xml",
                                         "scope": ["ledgerly"], "min_delta_pp": 20})
    assert ok["passed"] is True and ok["score"] == 1.0
    low = C.coverage_delta_min(tmp_path, {"before": "b.xml", "after": "a.xml",
                                          "scope": ["ledgerly"], "min_delta_pp": 50})
    assert low["passed"] is False
    # "After" report that drops the uncovered lines instead of testing them
    gamed = COBERTURA_BEFORE.replace('<line number="3" hits="0"/>', "").replace(
        '<line number="4" hits="0" branch="true" condition-coverage="0% (0/2)"/>', "")
    write(tmp_path, "gamed.xml", gamed)
    res = C.coverage_delta_min(tmp_path, {"before": "b.xml", "after": "gamed.xml",
                                          "scope": ["ledgerly"], "min_delta_pp": 10})
    assert res["passed"] is False and "measured lines fell" in res["details"]
    assert C.coverage_delta_min(tmp_path, {"before": "b.xml", "after": "a.xml",
                                           "scope": ["nothing/"], "min_delta_pp": 1})["passed"] is False


def test_diff_and_patch_checks(tmp_path):
    write(tmp_path, "ok.patch", PATCH_TESTS_ONLY)
    write(tmp_path, "bad.patch", PATCH_TOUCHES_SRC)
    write(tmp_path, "empty.patch", "")
    assert C.diff_test_paths_only(tmp_path, {"patch": "ok.patch"})["passed"] is True
    assert C.diff_test_paths_only(tmp_path, {"patch": "bad.patch"})["passed"] is False
    assert C.diff_test_paths_only(tmp_path, {"patch": "empty.patch"})["passed"] is False
    assert C.patch_size_max(tmp_path, {"patch": "ok.patch", "max_changed_lines": 400})["passed"] is True
    assert C.patch_size_max(tmp_path, {"patch": "ok.patch", "max_changed_lines": 3})["passed"] is False
    assert C.no_assertion_free_tests(tmp_path, {"patch": "ok.patch"})["passed"] is True
    hollow = PATCH_TESTS_ONLY.replace("+    assert convert(0, 2) == 0", "+    convert(0, 2)")
    write(tmp_path, "hollow.patch", hollow)
    res = C.no_assertion_free_tests(tmp_path, {"patch": "hollow.patch"})
    assert res["passed"] is False and "test_convert_zero" in res["details"]
    assert C.patch_secret_free(tmp_path, {"patch": "ok.patch"})["passed"] is True
    leaky = PATCH_TESTS_ONLY + ("diff --git a/tests/fixtures/k.pem b/tests/fixtures/k.pem\n"
                                "--- /dev/null\n+++ b/tests/fixtures/k.pem\n@@ -0,0 +1 @@\n"
                                "+-----BEGIN " + "RSA PRIVATE KEY-----\n")
    write(tmp_path, "leaky.patch", leaky)
    assert C.patch_secret_free(tmp_path, {"patch": "leaky.patch"})["passed"] is False
    assert C.diff_test_paths_only(tmp_path, {})["passed"] is False  # missing params


def _runs(ws: Path, n: int, flaky_on: int | None = None, prefix: str = "runs"):
    for i in range(1, n + 1):
        bad = flaky_on == i
        write(ws, f"{prefix}/run-{i:02d}.xml", junit([("a", "passed"), ("b", "failed" if bad else "passed")]))


def test_tests_stable(tmp_path):
    _runs(tmp_path, 10)
    assert C.tests_stable(tmp_path, {"runs": "runs/run-*.xml"})["passed"] is True
    assert C.tests_stable(tmp_path, {"runs": "runs/run-*.xml", "require_tests": ["::b"]})["passed"] is True
    assert C.tests_stable(tmp_path, {"runs": "runs/run-*.xml", "require_tests": ["::zz"]})["passed"] is False
    assert C.tests_stable(tmp_path, {"runs": "runs/run-*.xml", "min_runs": 11})["passed"] is False
    _runs(tmp_path, 10, flaky_on=7, prefix="flaky")
    res = C.tests_stable(tmp_path, {"runs": "flaky/run-*.xml"})
    assert res["passed"] is False and "flaky" in res["details"]


def test_flake_census_matches(tmp_path):
    _runs(tmp_path, 5, flaky_on=3)
    census = T.parse_test_results(tmp_path, paths="runs/run-*.xml", out="deliverables/census.json")
    assert census["flaky"] == ["tests.test_rates::b"]
    params = {"census": "deliverables/census.json", "runs": "runs/run-*.xml"}
    assert C.flake_census_matches(tmp_path, params)["passed"] is True
    forged = dict(census, flaky=[])  # hide the flaky test
    (tmp_path / "deliverables/census.json").write_text(json.dumps(forged))
    assert C.flake_census_matches(tmp_path, params)["passed"] is False


def test_mutation_score_min(tmp_path):
    write(tmp_path, "mut.json", json.dumps(MUTATION))
    ok = C.mutation_score_min(tmp_path, {"report": "mut.json", "min_score_pct": 50})
    assert ok["passed"] is True and ok["score"] == 0.5
    assert C.mutation_score_min(tmp_path, {"report": "mut.json", "min_score_pct": 60})["passed"] is False
    assert C.mutation_score_min(tmp_path, {"report": "mut.json", "min_score_pct": 10,
                                           "scope": ["other/"]})["passed"] is False


def test_targets_ranking_matches(tmp_path):
    write(tmp_path, "cov.xml", COBERTURA_BEFORE)
    write(tmp_path, "churn.csv", "path,commits\nledgerly/rates.py,12\nledgerly/util.py,40\n")
    T.rank_targets(tmp_path, coverage="cov.xml", churn="churn.csv", out="t.csv")
    params = {"targets": "t.csv", "coverage": "cov.xml", "churn": "churn.csv"}
    assert C.targets_ranking_matches(tmp_path, params)["passed"] is True
    rows = (tmp_path / "t.csv").read_text().splitlines()
    swapped = [rows[0], rows[2].replace("2,", "1,", 1), rows[1].replace("1,", "2,", 1)]
    (tmp_path / "t.csv").write_text("\n".join(swapped) + "\n")
    assert C.targets_ranking_matches(tmp_path, params)["passed"] is False
    T.rank_targets(tmp_path, coverage="cov.xml", churn="churn.csv", out="t.csv")
    text = (tmp_path / "t.csv").read_text().replace(",12,", ",99,")  # inflated churn
    (tmp_path / "t.csv").write_text(text)
    assert C.targets_ranking_matches(tmp_path, params)["passed"] is False
    # a top-1 list that skips the riskiest file
    (tmp_path / "t.csv").write_text(rows[0] + "\n" + rows[2].replace("2,", "1,", 1) + "\n")
    assert C.targets_ranking_matches(tmp_path, params)["passed"] is False


def test_check_defs_signature():
    for name, fn in C.CHECK_DEFS.items():
        out = fn(Path("."), {})
        assert set(out) == {"passed", "details", "score"} and out["passed"] is False, name


# --- manifest ----------------------------------------------------------------

PACK = Path(__file__).resolve().parents[2] / "specialists" / "test_coverage"

# Names the kit provides (agentkit contract v1).
KIT_TOOLS = {"read_document", "read_file", "write_file", "edit_file", "list_files", "search_files",
             "run_command", "http_fetch", "web_search", "record_source", "record_claim",
             "ask_client", "post_progress", "submit_milestone"}
KIT_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
              "json_valid", "csv_columns", "command_succeeds", "ledger_verified", "citations_resolve",
              "disclaimer_present", "rubric_grader", "human_signoff"}


def _manifest():
    return yaml.safe_load((PACK / "agent.yaml").read_text(encoding="utf-8"))


def test_manifest_parses_and_references_known_tools_and_checks():
    m = _manifest()
    assert m["schema_version"] == 1 and m["slug"] == "test-coverage" and m["profile"] == "code"
    domain_tools = {d["name"] for d in T.TOOL_DEFS}
    assert set(m["tools"]) <= KIT_TOOLS | domain_tools
    assert domain_tools <= set(m["tools"])  # every domain tool is exposed
    assert [ms["id"] for ms in m["milestones"]] == ["m1-baseline", "m2-characterization",
                                                    "m3-coverage-uplift"]
    for ms in m["milestones"]:
        assert ms["deliverables"] and all(d.startswith(f"deliverables/{ms['id']}/") for d in ms["deliverables"])
        for crit in ms["acceptance"]:
            assert crit["check"] in KIT_CHECKS | set(C.CHECK_DEFS), crit["check"]
            if crit["check"] == "rubric_grader":
                assert (PACK / crit["params"]["rubric"]).is_file()
        kinds = {c.get("kind", "automated") for c in ms["acceptance"]}
        assert "human" in kinds and "automated" in kinds
    assert m["human_gate"]["required"] is False
    assert m["listing"]["pricing"]["currency"] == "USDC"
    assert m["models"]["primary"].startswith("anthropic:")
    for rel in [m["prompts"]["system"], *m["prompts"]["include"]]:
        assert (PACK / rel).is_file()


def test_rubrics_are_well_formed():
    for path in (PACK / "rubrics").glob("*.yaml"):
        r = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert r["name"] and 0 < r["threshold"] <= 1
        ids = [c["id"] for c in r["criteria"]]
        assert len(ids) == len(set(ids)) and all(c["weight"] > 0 and c["description"] for c in r["criteria"])


# --- eval fixtures -----------------------------------------------------------

FIXTURE = PACK / "evals" / "fixtures" / "brambleway-freight"


def test_fixture_baseline_reports_are_consistent():
    cov = T.summarize_coverage((FIXTURE / "coverage-baseline.xml").read_text(encoding="utf-8"))
    assert cov["totals"]["lines_total"] == 21 and cov["totals"]["lines_covered"] == 11
    ranked = T.rank_targets(FIXTURE, coverage="coverage-baseline.xml", churn="churn.csv")["targets"]
    assert ranked[0]["path"] == "brambleway/fares.py"
    census = T.census_from_runs([T.parse_junit_text(p.read_text(encoding="utf-8"))
                                 for p in sorted((FIXTURE / "runs").glob("run-*.xml"))])
    assert census["runs"] == 5 and census["flaky"] == ["tests.test_zones::test_zone_a"]
    assert C.mutation_score_min(FIXTURE, {"report": "mutation.json", "min_score_pct": 60})["passed"]


def test_fixture_patches_separate_good_from_bad():
    for check in ("diff_test_paths_only", "no_assertion_free_tests", "patch_secret_free"):
        assert C.CHECK_DEFS[check](FIXTURE, {"patch": "good.patch"})["passed"] is True, check
    assert C.diff_test_paths_only(FIXTURE, {"patch": "bad.patch"})["passed"] is False
    assert C.no_assertion_free_tests(FIXTURE, {"patch": "bad.patch"})["passed"] is False


def test_eval_cases_target_manifest_milestones():
    ids = {m["id"] for m in _manifest()["milestones"]}
    cases = sorted((PACK / "evals" / "cases").glob("*.json"))
    assert cases
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert {"name", "brief", "milestone", "notes"} <= set(case)
        assert case["milestone"] in ids and case["brief"]["specialist"] == "test-coverage"


def test_patch_parser_cannot_be_fooled_by_headers_inside_hunks(tmp_path):
    # Plain multi-file diff (no "diff --git" lines): the second file must be seen.
    plain = ("--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1,1 +1,2 @@\n x = 1\n+assert x\n"
             "--- a/ledgerly/rates.py\n+++ b/ledgerly/rates.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n")
    write(tmp_path, "plain.patch", plain)
    assert C.diff_test_paths_only(tmp_path, {"patch": "plain.patch"})["passed"] is False
    # A removed SQL comment line ("-- x" -> "--- x") inside a hunk is a removal, not a header.
    sql = ("diff --git a/tests/data.sql b/tests/data.sql\n--- a/tests/data.sql\n+++ b/tests/data.sql\n"
           "@@ -1,2 +1,1 @@\n--- a/ledgerly/rates.py\n keep\n")
    files = T.parse_patch(sql)
    assert len(files) == 1 and files[0]["removed"] == 1
    # Dot-dot paths never count as test paths.
    sneaky = PATCH_TESTS_ONLY.replace("tests/test_rates.py", "tests/../ledgerly/rates.py")
    write(tmp_path, "sneaky.patch", sneaky)
    assert C.diff_test_paths_only(tmp_path, {"patch": "sneaky.patch"})["passed"] is False
    write(tmp_path, "badhunk.patch", "--- a/x\n+++ b/x\n@@ nonsense @@\n")
    assert C.diff_test_paths_only(tmp_path, {"patch": "badhunk.patch"})["passed"] is False


def test_run_matrix_clears_stale_runs_and_bad_globs_fail(tmp_path):
    (tmp_path / "repo").mkdir()
    write(tmp_path, "r/run-07.xml", junit([("old", "failed")]))

    def fake_run(argv, *, cwd=None, timeout=None):
        Path(argv[1]).write_text(junit([("a", "passed")]))
        return {"exit_code": 0}

    out = T.run_test_matrix(tmp_path, run=fake_run, runs=2, runs_dir="r", argv=["pytest", "{junit}"])
    assert out["runs"] == 2 and out["all_green"] and not (tmp_path / "r/run-07.xml").exists()
    assert C.tests_stable(tmp_path, {"runs": "/abs/*.xml", "min_runs": 1})["passed"] is False
