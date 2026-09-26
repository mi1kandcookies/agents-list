"""test-coverage specialist domain pack: tools, checks and manifest wiring."""
import json
from pathlib import Path

import pytest

from agentkit.errors import ToolError
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
@@ -0,0 +1,6 @@
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
    with pytest.raises(ToolError):
        T.parse_coverage(tmp_path, path="../outside.xml")
    write(tmp_path, "inputs/coverage.xml", COBERTURA_BEFORE)
    with pytest.raises(ToolError):
        T.parse_coverage(tmp_path, path="inputs/coverage.xml", out="inputs/summary.json")
    with pytest.raises(ToolError):
        T.parse_coverage(tmp_path, path="inputs/coverage.xml", out=".agentkit/x.json")


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


def test_export_patch_writes_git_diff(tmp_path):
    (tmp_path / "repo").mkdir()
    seen = []

    def fake_run(argv, *, cwd=None, timeout=None):
        seen.append(argv)
        return {"exit_code": 0, "stdout": PATCH_TESTS_ONLY if "diff" in argv else "", "stderr": ""}

    out = T.export_patch(tmp_path, run=fake_run, out="deliverables/m3-coverage-uplift/repo.patch")
    assert out["test_only"] and seen[0][:3] == ["git", "add", "-N"]
    assert (tmp_path / "deliverables/m3-coverage-uplift/repo.patch").read_text() == PATCH_TESTS_ONLY
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
        return {"exit_code": 0, "stdout": "a.py\nb.py\n\na.py\n", "stderr": ""}

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
