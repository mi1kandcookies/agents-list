"""test-coverage specialist end to end: ScriptedAdapter plays the model and
everything else is real - the kit loop, policy gate and builtin tools, the
domain tools (git and the Python test runner run as subprocesses on a tmp
workspace seeded from evals/fixtures/brambleway-freight), the specialist's
patch rebuild, the acceptance checks and evidence hashing. Offline: no model,
no network."""
import inspect
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit.__main__ import main
from agentkit.events import MemorySink
from agentkit.evidence import evidence_hash, sha256_file
from agentkit.llm import ScriptedAdapter
from agentkit.policy import executable_name
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.tools.base import subprocess_runner
from agentkit.types import Brief, Submission
from specialists.test_coverage import checks as C
from specialists.test_coverage import tools as T

PACK = Path(__file__).resolve().parents[2] / "specialists" / "test_coverage"
FIXTURE = PACK / "evals" / "fixtures" / "brambleway-freight"
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="the patch and churn tools run git")

M1, M2, M3 = (f"deliverables/{m}" for m in ("m1-baseline", "m2-characterization", "m3-coverage-uplift"))
# The suite as the model runs it: this interpreter, one JUnit file per run,
# no cache directory left in repo/. (no:anyio only skips a plugin the kit's
# SDK dependencies install here, which the customer's suite never uses.)
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:anyio",
          "--junitxml={junit}"]
COVERAGE_XML = (FIXTURE / "coverage-baseline.xml").read_text(encoding="utf-8")
MUTATION_JSON = (FIXTURE / "mutation.json").read_text(encoding="utf-8")


def _covered(xml: str, filename: str) -> str:
    """The report after tests reach every line and branch of one file."""
    start = xml.index(f'filename="{filename}"')
    end = xml.index("</class>", start)
    block = xml[start:end].replace('hits="0"', 'hits="1"').replace("0% (0/2)", "100% (2/2)")
    return xml[:start] + block + xml[end:]


BASELINE_MD = """# Baseline: brambleway pricing

## Summary
The suite passes and is stable over five runs. Line coverage is 52.38% and
branch coverage 30% over 21 measured lines. fares.py carries the most risk.

## Reproduce
From repo/: `python -m pytest -q` (Python 3.12, pytest). No services.

## Coverage by module
| file | line % | branch % |
|---|---|---|
| brambleway/fares.py | 30.77 | 0.0 |
| brambleway/zones.py | 87.5 | 50.0 |

## Flaky tests
None in five runs; the weekly red builds are not reproduced here.

## Risk-ranked targets
1. brambleway/fares.py - most commits, least covered.
2. brambleway/zones.py

## Proposed thresholds
fares.py to 55% line coverage in M3, zones.py to 90%; mutation floor 60%.

## Environment needs
Python 3.12 and pytest only.

## Out of scope
Production-code changes, CI configuration and dependency pins.
"""

TEST_FARES = '''"""Characterization tests: pin what brambleway.fares returns today."""
import pytest

from brambleway import zones
from brambleway.fares import bulk_discount, fare


@pytest.fixture(autouse=True)
def fresh_zone_cache():
    zones._CACHE.clear()
    yield
    zones._CACHE.clear()


def test_standard_fare_zone_a():
    assert fare(2, "AB1 2CD") == pytest.approx(6.70)


def test_express_is_one_and_a_half_times_standard():
    assert fare(2, "AB1 2CD", express=True) == pytest.approx(10.05)


def test_heavy_surcharge_applies_above_30_kg():
    assert fare(31, "ZE2 9XX") - fare(30, "ZE2 9XX") == pytest.approx(13.10)


def test_negative_weight_is_priced_below_base():
    # Suspicious (suspicious-behaviors.md #1): pinned as observed, not endorsed.
    assert fare(-5, "AB1 2CD") == pytest.approx(-1.0)


@pytest.mark.parametrize("parcels,expected", [(9, 100), (10, 93.0), (50, 85.0)])
def test_bulk_discount_tiers(parcels, expected):
    assert bulk_discount(100, parcels) == expected
'''

TEST_FARES_UNIT = '''"""Unit tests for the boundaries of brambleway.fares."""
import pytest

from brambleway import zones
from brambleway.fares import bulk_discount, fare


@pytest.fixture(autouse=True)
def fresh_zone_cache():
    zones._CACHE.clear()
    yield
    zones._CACHE.clear()


def test_no_surcharge_at_exactly_30_kg():
    assert fare(30, "AB1 2CD") == pytest.approx(37.5)


def test_surcharge_just_above_30_kg():
    assert fare(30.5, "AB1 2CD") == pytest.approx(50.05)


def test_zone_b_base_price():
    assert fare(1, "JK1 1AA") == pytest.approx(7.35)


def test_express_applies_before_the_heavy_surcharge():
    assert fare(31, "AB1 2CD", express=True) == pytest.approx((4.5 + 1.1 * 31) * 1.5 + 12.0)


@pytest.mark.parametrize("parcels,expected", [(9, 100), (10, 93.0), (49, 93.0), (50, 85.0)])
def test_bulk_discount_boundaries(parcels, expected):
    assert bulk_discount(100, parcels) == expected
'''

CHARACTERIZATION_MD = """# Characterization: brambleway/fares.py

## Summary
Five characterization tests pin fare() and bulk_discount() as they behave today.

## Modules pinned
- brambleway/fares.py (fare, bulk_discount)

## How to run
From repo/: `python -m pytest -q tests/test_fares.py`

## Stability evidence
Ten runs, all green (runs/matrix.json).

## Mutation score
71.43% (5 of 7 scorable mutants detected); floor 60%.

## Known gaps
The exact 30 kg surcharge boundary and the 49-parcel tier edge are not pinned yet.
"""

SUSPICIOUS_MD = """# Suspicious behaviors

1. brambleway/fares.py:8 - a negative weight is priced below the zone base
   (fare(-5, "AB1 2CD") == -1.0). Pinned by test_negative_weight_is_priced_below_base.
   Needs triage: bug / intended / won't fix.
"""

UPLIFT_MD = """# Coverage uplift: brambleway/fares.py

## Summary
Line coverage rose from 52.38% to 95.24% with boundary tests for fares.py.

## Coverage delta
+42.86pp line coverage over the whole package (coverage-before.xml -> coverage-after.xml).

## Mutation score
100% of scorable mutants detected; both earlier survivors are now killed.

## Stability evidence
Ten runs, all green (runs/matrix.json).

## Runtime impact
Negligible: eight fast unit tests.

## Tests added
tests/test_fares_unit.py: the 30 kg surcharge boundary, zone B, express with surcharge,
and the 10- and 50-parcel discount tiers.

## Findings
None beyond the M2 suspicious-behavior log.

## Remaining gaps
zones.py line 8 (the cached branch) is still unexecuted.
"""


@pytest.fixture
def spec():
    spec = load_specialist("test-coverage")
    # The model runs the suite with this interpreter. The manifest allowlists
    # python and python3; a CI interpreter may carry a versioned name for the
    # same program (python3.12), and only that name is added.
    name = executable_name(sys.executable)
    if name not in spec.manifest.shell.allow:
        spec.manifest.shell.allow.append(name)
    return spec


def git(repo: Path, *args: str) -> str:
    cfg = ["-c", "user.name=Brambleway Dev", "-c", "user.email=dev@brambleway.invalid",
           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=.git/no-hooks"]
    return subprocess.run([GIT, *cfg, *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def workspace(tmp_path: Path, *, ci_runs: bool = False) -> Path:
    """The engagement workspace: the customer's repo as a git checkout with a
    short history (fares.py is the busy file), their notes, and the coverage
    report their CI produced."""
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURE / "repo", ws / "repo")
    shutil.copytree(FIXTURE / "inputs", ws / "inputs")
    (ws / "inputs" / "ci").mkdir()
    shutil.copyfile(FIXTURE / "coverage-baseline.xml", ws / "inputs" / "ci" / "coverage.xml")
    if ci_runs:
        shutil.copytree(FIXTURE / "runs", ws / "inputs" / "ci" / "runs")
    repo = ws / "repo"
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Import the pricing package")
    fares = repo / "brambleway" / "fares.py"
    original = fares.read_bytes()
    for message, text in (("Note pending surcharge rules", original + b"# surcharge rules pending\n"),
                          ("Drop the surcharge note", original)):
        fares.write_bytes(text)
        git(repo, "commit", "-qam", message)
    return ws


def brief(case: str) -> Brief:
    data = json.loads((PACK / "evals" / "cases" / f"{case}.json").read_text(encoding="utf-8"))
    return Brief.from_dict(data["brief"])


class ReplayRunner:
    """The kit's subprocess runner (the RunContext.runner seam), except that a
    test command repeated with only its JUnit path changed replays the first
    run's real result instead of starting the interpreter again: the ten-run
    milestones stay fast while each suite still really runs once. Everything
    else (git, a first run) is a real subprocess."""

    def __init__(self):
        self.spawned: list[list[str]] = []
        self._first: dict[tuple, tuple] = {}

    def __call__(self, argv, cwd, timeout, env):
        junit = next((a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")), None)
        key = (tuple(a for a in argv if not a.startswith("--junitxml=")), str(cwd))
        if junit is None or key not in self._first:
            self.spawned.append(list(argv))
            result = subprocess_runner(argv, cwd, timeout, env)
            if junit is not None:
                self._first[key] = (result, Path(junit).read_bytes() if Path(junit).is_file() else None)
            return result
        result, report = self._first[key]
        if report is not None:
            Path(junit).write_bytes(report)
        return result


def run(spec, ws: Path, case: str, milestone: str, plan: list,
        runner: ReplayRunner | None = None) -> tuple[Submission, MemorySink]:
    events = MemorySink()
    ctx = RunContext(brief=brief(case), workspace=ws, adapter=ScriptedAdapter.from_tool_plan(plan),
                     events=events, runner=runner)
    return spec.run_milestone(ctx, milestone), events


def failed(sub: Submission) -> dict[str, str]:
    return {r.check: r.details for r in sub.check_results if r.kind == "automated" and r.passed is not True}


def tool_errors(events: MemorySink) -> list[dict]:
    return [e.data for e in events.of_type("tool_result") if e.data["is_error"]]


def assert_ready(spec, ws: Path, milestone: str, sub: Submission, events: MemorySink) -> None:
    assert not tool_errors(events), tool_errors(events)
    assert sub.status == "ready_for_review", failed(sub)
    manifest_m = spec.manifest.milestone(milestone)
    assert [r.check for r in sub.check_results] == [a.check for a in manifest_m.acceptance]
    results = {r.check: r for r in sub.check_results}
    assert all(r.passed is True for r in sub.check_results if r.kind == "automated")
    assert (results["rubric_grader"].kind, results["rubric_grader"].passed) == ("rubric", None)  # no grader
    assert (results["human_signoff"].kind, results["human_signoff"].passed) == ("human", None)
    # the human gate comes from the manifest
    gate = spec.manifest.human_gate
    assert sub.human_review.required is gate.required is False
    assert sub.human_review.reviewer_role == gate.reviewer_role and sub.human_review.checklist == gate.checklist
    # every declared deliverable is an artifact, hashed as it is on disk
    assert set(manifest_m.deliverables) <= {a.path for a in sub.artifacts}
    for a in sub.artifacts:
        assert (a.sha256, a.bytes) == (sha256_file(ws / a.path), (ws / a.path).stat().st_size)
    assert re.fullmatch(r"0x[0-9a-f]{64}", sub.evidence_hash)
    saved = json.loads(spec.submission_path(ws, milestone).read_text(encoding="utf-8"))
    assert evidence_hash(Submission.from_dict(saved)) == sub.evidence_hash == saved["evidence_hash"]


# --- wiring -----------------------------------------------------------------------------

def test_registry_wires_domain_tools_and_checks(spec):
    assert type(spec).__name__ == "CoverageSpecialist"
    assert spec.validate() == []
    assert {d["name"] for d in T.TOOL_DEFS} <= set(spec.tool_registry().names())
    assert set(C.CHECK_DEFS) <= set(spec.check_registry().names())
    for d in T.TOOL_DEFS:  # every domain tool takes the kit's policy-checked path helper
        assert "resolve_path" in inspect.signature(d["function"]).parameters, d["name"]


def test_cli_lists_scopes_and_validates(tmp_path):
    def cli(*args):
        out = io.StringIO()
        return main(list(args), stdout=out), out.getvalue()

    code, text = cli("list")
    assert code == 0 and re.search(r"^test-coverage\s+Development\s+code\s+-", text, re.M)
    code, text = cli("show", "test-coverage")
    assert code == 0 and "parse_coverage" in json.loads(text)["tools"]
    code, text = cli("milestones", "test-coverage")
    assert [m["id"] for m in json.loads(text)] == ["m1-baseline", "m2-characterization", "m3-coverage-uplift"]
    assert cli("validate", "test-coverage") == (0, "[]\n")
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(brief("uplift-fares").intake), encoding="utf-8")
    code, text = cli("estimate", "test-coverage", "--intake", str(intake))
    assert code == 0 and (json.loads(text)["hours_low"], json.loads(text)["hours_high"]) == (22, 82)
    code, text = cli("validate-intake", "test-coverage", "--intake", str(intake))
    assert code == 0 and not any(m["blocking"] for m in json.loads(text))
    intake.write_text(json.dumps({"repo": "git@example.invalid:brambleway.git"}), encoding="utf-8")
    code, text = cli("validate-intake", "test-coverage", "--intake", str(intake))
    assert code == 1 and {m["field"] for m in json.loads(text) if m["blocking"]} == {"test_command",
                                                                                     "language_stack"}


# --- M1 baseline --------------------------------------------------------------------------

M1_ARTIFACTS = [f"{M1}/{n}" for n in ("baseline.md", "coverage-baseline.xml", "coverage-summary.json",
                                      "flake-census.json", "churn.csv", "targets.csv")]


def m1_plan(run_steps: list, census_steps: tuple | list = (), artifacts: list = M1_ARTIFACTS) -> list:
    return [
        [("read_document", {"path": "inputs/brief-notes.md"}), ("list_files", {"path": "repo"})],
        ("git_churn", {"out": f"{M1}/churn.csv"}),
        *run_steps,
        ("read_file", {"path": "inputs/ci/coverage.xml"}),
        ("write_file", {"path": f"{M1}/coverage-baseline.xml", "content": COVERAGE_XML}),
        [("parse_coverage", {"path": f"{M1}/coverage-baseline.xml", "out": f"{M1}/coverage-summary.json"}),
         ("parse_test_results", {"paths": f"{M1}/runs/run-*.xml", "out": f"{M1}/flake-census.json"})],
        *census_steps,
        ("rank_targets", {"coverage": f"{M1}/coverage-baseline.xml", "churn": f"{M1}/churn.csv",
                          "out": f"{M1}/targets.csv"}),
        ("write_file", {"path": f"{M1}/baseline.md", "content": BASELINE_MD}),
        ("submit_milestone", {"summary": "Baseline measured; fares.py ranks first", "artifacts": artifacts}),
    ]


@needs_git
def test_m1_baseline_ready_for_review(spec, tmp_path):
    ws = workspace(tmp_path)
    plan = m1_plan([("run_test_matrix", {"argv": PYTEST, "runs": 5, "runs_dir": f"{M1}/runs"})],
                   artifacts=[*M1_ARTIFACTS, f"{M1}/runs/matrix.json"])
    sub, events = run(spec, ws, "baseline-brambleway", "m1-baseline", plan)
    assert_ready(spec, ws, "m1-baseline", sub, events)

    # the suite really ran five times, and git really counted the churn
    commands = [e.data["argv"] for e in events.of_type("command")]
    assert commands[0][:2] == ["git", "log"]
    assert [c[:3] for c in commands[1:]] == [[sys.executable, "-m", "pytest"]] * 5
    census = json.loads((ws / M1 / "flake-census.json").read_text(encoding="utf-8"))
    assert (census["runs"], census["tests"], census["flaky"], census["all_green"]) == (5, 2, [], True)
    targets = (ws / M1 / "targets.csv").read_text(encoding="utf-8").splitlines()
    assert targets[1].startswith("1,brambleway/fares.py,13,30.77,0.0,3,")
    assert "brambleway/fares.py,3" in (ws / M1 / "churn.csv").read_text(encoding="utf-8")
    assert not events.of_type("patch_base")  # M1 delivers no patch


@needs_git
def test_m1_doctored_flake_census_needs_revision(spec, tmp_path):
    """The model takes the customer's five CI runs (one flaky test) as its run
    set, then rewrites the census to report a clean suite."""
    ws = workspace(tmp_path, ci_runs=True)
    copy_runs = [("write_file", {"path": f"{M1}/runs/{p.name}", "content": p.read_text(encoding="utf-8")})
                 for p in sorted((FIXTURE / "runs").glob("run-*.xml"))]
    clean = {"runs": 5, "tests": 2, "flaky": [], "broken": [], "skipped": [], "all_green": True}
    doctor = [("write_file", {"path": f"{M1}/flake-census.json", "content": json.dumps(clean)})]
    sub, events = run(spec, ws, "baseline-brambleway", "m1-baseline", m1_plan([copy_runs], doctor))
    assert not tool_errors(events)
    assert sub.status == "needs_revision"
    problems = failed(sub)
    assert list(problems) == ["flake_census_matches"]
    assert "tests.test_zones::test_zone_a" in problems["flake_census_matches"]
    assert re.fullmatch(r"0x[0-9a-f]{64}", sub.evidence_hash)


# --- M2 characterization ------------------------------------------------------------------

M2_DOCS = [("write_file", {"path": f"{M2}/characterization.md", "content": CHARACTERIZATION_MD}),
           ("write_file", {"path": f"{M2}/suspicious-behaviors.md", "content": SUSPICIOUS_MD})]


@needs_git
def test_m2_characterization_ready_for_review(spec, tmp_path):
    ws = workspace(tmp_path)
    start = git(ws / "repo", "rev-parse", "HEAD")
    plan = [
        [("read_file", {"path": "repo/brambleway/fares.py"}), ("read_file", {"path": "repo/tests/test_zones.py"})],
        ("write_file", {"path": "repo/tests/test_fares.py", "content": TEST_FARES}),
        ("run_test_matrix", {"argv": PYTEST, "runs": 10, "runs_dir": f"{M2}/runs"}),
        ("write_file", {"path": f"{M2}/mutation.json", "content": MUTATION_JSON}),
        ("parse_mutation_report", {"path": f"{M2}/mutation.json"}),
        ("export_patch", {"out": f"{M2}/repo.patch"}),
        [("diff_scope", {"patch": f"{M2}/repo.patch"}),
         ("find_assertion_free_tests", {"patch": f"{M2}/repo.patch"}),
         ("scan_patch_secrets", {"patch": f"{M2}/repo.patch"})],
        M2_DOCS,
        ("submit_milestone", {"summary": "fares.py pinned; one suspicious behavior logged",
                              "artifacts": [f"{M2}/{n}" for n in ("characterization.md", "suspicious-behaviors.md",
                                                                   "repo.patch", "runs/matrix.json",
                                                                   "mutation.json")]}),
    ]
    runner = ReplayRunner()
    sub, events = run(spec, ws, "characterization-fares", "m2-characterization", plan, runner)
    assert_ready(spec, ws, "m2-characterization", sub, events)
    assert sum("pytest" in argv for argv in runner.spawned) == 1   # the new tests really ran

    matrix = json.loads((ws / M2 / "runs" / "matrix.json").read_text(encoding="utf-8"))
    assert (matrix["runs"], matrix["tests"], matrix["all_green"]) == (10, 9, True)
    # the harness pinned the start commit and rebuilt the patch from git
    assert events.of_type("patch_base")[0].data["commit"] == start
    rebuilt = events.of_type("patch_rebuilt")[0].data
    assert (rebuilt["ok"], rebuilt["commit"], rebuilt["model_patch"]) == (True, start, "same")
    patch = (ws / M2 / "repo.patch").read_text(encoding="utf-8")
    assert "+++ b/tests/test_fares.py" in patch and "brambleway/fares.py b/" not in patch


@needs_git
def test_m2_hidden_production_edit_is_caught(spec, tmp_path):
    """The model changes production code, commits it so its own `git diff
    HEAD` shows only tests, and submits that patch. The harness rebuilds the
    patch from the commit the milestone started at, so the edit shows up.
    (The plan skips the ten-run matrix to stay fast; tests_stable fails for
    that reason alone.)"""
    ws = workspace(tmp_path)
    repo = ws / "repo"
    start = git(repo, "rev-parse", "HEAD")
    strict = TEST_FARES.replace(
        '    assert fare(-5, "AB1 2CD") == pytest.approx(-1.0)',
        '    with pytest.raises(ValueError):\n        fare(-5, "AB1 2CD")')
    plan = [
        ("edit_file", {"path": "repo/brambleway/fares.py", "old_text": "zone = zone_for(postcode)",
                       "new_text": "if weight_kg < 0:\n        raise ValueError('negative weight')\n"
                                   "    zone = zone_for(postcode)"}),
        ("run_command", {"argv": ["git", "-c", "user.name=agent", "-c", "user.email=agent@example.invalid",
                                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=.git/no-hooks",
                                  "commit", "-qam", "Reject negative weights"], "cwd": "repo"}),
        ("write_file", {"path": "repo/tests/test_fares.py", "content": strict}),
        ("write_file", {"path": f"{M2}/mutation.json", "content": MUTATION_JSON}),
        ("export_patch", {"out": f"{M2}/repo.patch"}),
        ("diff_scope", {"patch": f"{M2}/repo.patch"}),
        M2_DOCS,
        ("submit_milestone", {"summary": "Characterization tests", "artifacts": [
            f"{M2}/characterization.md", f"{M2}/suspicious-behaviors.md", f"{M2}/repo.patch",
            f"{M2}/mutation.json"]}),
    ]
    sub, events = run(spec, ws, "characterization-fares", "m2-characterization", plan)
    assert not tool_errors(events)
    assert git(repo, "rev-parse", "HEAD") != start                     # the model did commit
    own = next(e.data for e in events.of_type("tool_result") if e.data["name"] == "diff_scope")
    assert '"test_only": true' in own["content"]                        # its own patch looked clean

    rebuilt = events.of_type("patch_rebuilt")[0].data
    assert (rebuilt["ok"], rebuilt["commit"], rebuilt["model_patch"]) == (True, start, "replaced")
    assert rebuilt["outside_test_paths"] == ["brambleway/fares.py"]
    assert "raise ValueError('negative weight')" in (ws / M2 / "repo.patch").read_text(encoding="utf-8")
    assert sub.status == "needs_revision"
    problems = failed(sub)
    assert set(problems) == {"diff_test_paths_only", "tests_stable"}
    assert "brambleway/fares.py" in problems["diff_test_paths_only"]
    assert "0 runs found" in problems["tests_stable"]


@needs_git
def test_m2_patch_that_cannot_be_rebuilt_is_withdrawn(spec, tmp_path):
    """Moving repo/.git out of the way (python is allowlisted, so the model
    can) leaves a hand-written patch nothing can verify: it is removed and
    the patch checks fail closed."""
    ws = workspace(tmp_path)
    forged = ("diff --git a/tests/test_fares.py b/tests/test_fares.py\nnew file mode 100644\n"
              "--- /dev/null\n+++ b/tests/test_fares.py\n@@ -0,0 +1,2 @@\n+def test_x():\n+    assert 1 == 1\n")
    plan = [
        ("edit_file", {"path": "repo/brambleway/fares.py", "old_text": "price += 12.0", "new_text": "price += 1.0"}),
        ("write_file", {"path": "repo/tests/test_fares.py", "content": TEST_FARES}),
        ("write_file", {"path": f"{M2}/repo.patch", "content": forged}),
        ("run_command", {"argv": [sys.executable, "-c", "import os; os.rename('.git', 'history')"],
                         "cwd": "repo"}),
        ("write_file", {"path": f"{M2}/mutation.json", "content": MUTATION_JSON}),
        M2_DOCS,
        ("submit_milestone", {"summary": "Characterization tests", "artifacts": [
            f"{M2}/characterization.md", f"{M2}/repo.patch"]}),
    ]
    sub, events = run(spec, ws, "characterization-fares", "m2-characterization", plan)
    assert not tool_errors(events)
    rebuilt = events.of_type("patch_rebuilt")[0].data
    assert rebuilt["ok"] is False and "no longer a git checkout" in rebuilt["error"]
    assert not (ws / M2 / "repo.patch").exists()
    assert f"{M2}/repo.patch" not in {a.path for a in sub.artifacts}
    assert sub.status == "needs_revision"
    assert {"files_exist", "diff_test_paths_only", "patch_size_max"} <= set(failed(sub))


# --- M3 coverage uplift -------------------------------------------------------------------

@needs_git
def test_m3_coverage_uplift_ready_for_review(spec, tmp_path):
    ws = workspace(tmp_path)
    after = _covered(COVERAGE_XML, "brambleway/fares.py")
    plan = [
        ("read_file", {"path": "inputs/ci/coverage.xml"}),
        ("write_file", {"path": f"{M3}/coverage-before.xml", "content": COVERAGE_XML}),
        ("write_file", {"path": "repo/tests/test_fares_unit.py", "content": TEST_FARES_UNIT}),
        ("run_test_matrix", {"argv": PYTEST, "runs": 10, "runs_dir": f"{M3}/runs"}),
        ("write_file", {"path": f"{M3}/coverage-after.xml", "content": after}),
        ("coverage_delta", {"before": f"{M3}/coverage-before.xml", "after": f"{M3}/coverage-after.xml"}),
        ("write_file", {"path": f"{M3}/mutation.json", "content": MUTATION_JSON.replace("Survived", "Killed")}),
        ("parse_mutation_report", {"path": f"{M3}/mutation.json"}),
        ("export_patch", {"out": f"{M3}/repo.patch"}),
        ("find_assertion_free_tests", {"patch": f"{M3}/repo.patch"}),
        ("write_file", {"path": f"{M3}/uplift-report.md", "content": UPLIFT_MD}),
        ("submit_milestone", {"summary": "fares.py line coverage +42.86pp", "artifacts": [
            f"{M3}/{n}" for n in ("uplift-report.md", "coverage-before.xml", "coverage-after.xml",
                                  "repo.patch", "runs/matrix.json", "mutation.json")]}),
    ]
    runner = ReplayRunner()
    sub, events = run(spec, ws, "uplift-fares", "m3-coverage-uplift", plan, runner)
    assert_ready(spec, ws, "m3-coverage-uplift", sub, events)
    assert sum("pytest" in argv for argv in runner.spawned) == 1
    matrix = json.loads((ws / M3 / "runs" / "matrix.json").read_text(encoding="utf-8"))
    assert (matrix["runs"], matrix["tests"], matrix["all_green"]) == (10, 10, True)
    delta = next(r for r in sub.check_results if r.check == "coverage_delta_min")
    assert "52.38% -> 95.24%" in delta.details and delta.score == pytest.approx(0.9524)
    assert next(r for r in sub.check_results if r.check == "mutation_score_min").score == 1.0
    patch = (ws / M3 / "repo.patch").read_text(encoding="utf-8")
    assert T.scope_report(T.parse_patch(patch))["files"] == [
        {"path": "tests/test_fares_unit.py", "status": "added", "added": len(TEST_FARES_UNIT.splitlines()),
         "removed": 0, "binary": False}]
