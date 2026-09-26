"""
agentkit/evals.py - run a specialist's golden cases against a live model.

Cases live in specialists/<package>/evals/cases/*.json:

    {"name": "...",                    # optional; defaults to the file name
     "milestone": "m1-...",            # required, a milestone id
     "brief": {...},                   # optional Brief fields; engagement_id,
                                       # specialist and objective are filled in
     "notes": "...",                   # optional; the objective if the brief has none
     "fixture": "dir",                 # optional: evals/fixtures/<dir>
                                       # ("evals/fixtures/<dir>" also accepted)
     "fixtures": {"inputs/a.txt": "a.txt"}}   # optional: dest -> evals/fixtures/<src>
                                       # (a string is read as "fixture")

A "fixture" directory that already contains inputs/ or repo/ is copied as the
workspace; otherwise its files go under inputs/. Each case runs in its own
workspace and reports status, per-check results, usage, the models that
served it and the evidence hash.
A case that does not match this schema raises AgentKitError naming the file.
This needs model access and is not part of CI; offline tests drive the same
code with ScriptedAdapter.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentkit.errors import AgentKitError
from agentkit.events import EventSink, NullSink
from agentkit.specialist import RunContext, Specialist
from agentkit.types import Brief


@dataclass
class EvalCase:
    name: str
    milestone: str
    brief: dict[str, Any]
    notes: str = ""
    fixture: str | None = None
    fixtures: dict[str, str] = field(default_factory=dict)
    path: Path | None = None


def evals_dir(spec: Specialist) -> Path:
    return spec.manifest.resolve("evals")


_CASE_KEYS = {"name", "milestone", "brief", "notes", "fixture", "fixtures"}
_FIXTURE_PREFIX = "evals/fixtures/"


def parse_case(data: Any, path: Path) -> EvalCase:
    """One eval case, validated against the schema in the module docstring."""
    def bad(message: str) -> AgentKitError:
        return AgentKitError(f"eval case {path.name}: {message}")

    if not isinstance(data, dict):
        raise bad("must be a JSON object")
    unknown = sorted(set(data) - _CASE_KEYS)
    if unknown:
        raise bad(f"unknown key(s) {', '.join(unknown)} (expected {', '.join(sorted(_CASE_KEYS))})")
    milestone = data.get("milestone")
    if not isinstance(milestone, str) or not milestone:
        raise bad("'milestone' must be a milestone id")
    brief, notes, name = data.get("brief", {}), data.get("notes", ""), data.get("name") or path.stem
    if not isinstance(brief, dict):
        raise bad("'brief' must be an object")
    if not isinstance(notes, str) or not isinstance(name, str):
        raise bad("'name' and 'notes' must be strings")
    fixture, fixtures = data.get("fixture"), data.get("fixtures") or {}
    if isinstance(fixtures, str):
        if fixture:
            raise bad("give one fixture directory, not both 'fixture' and a string 'fixtures'")
        fixture, fixtures = fixtures, {}
    if fixture is not None:
        if not isinstance(fixture, str) or not fixture:
            raise bad("'fixture' must be a directory name under evals/fixtures/")
        fixture = fixture.replace("\\", "/")
        if fixture.startswith(_FIXTURE_PREFIX):
            fixture = fixture[len(_FIXTURE_PREFIX):]
    if not isinstance(fixtures, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                 for k, v in fixtures.items()):
        raise bad("'fixtures' must map workspace paths to files under evals/fixtures/")
    return EvalCase(name=name, milestone=milestone, brief=dict(brief), notes=notes,
                    fixture=fixture, fixtures=dict(fixtures), path=path)


def load_cases(spec: Specialist) -> list[EvalCase]:
    cases = []
    for path in sorted((evals_dir(spec) / "cases").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentKitError(f"eval case {path.name}: invalid JSON: {exc}") from None
        cases.append(parse_case(data, path))
    return cases


def case_brief(spec: Specialist, case: EvalCase) -> Brief:
    data = {"engagement_id": f"eval-{case.name}", "specialist": spec.slug,
            "objective": case.notes or case.name, **case.brief}
    brief = Brief.from_dict(data)
    if not brief.milestones:
        brief.milestones = spec.propose_milestones(brief.intake)
    return brief


def prepare_workspace(spec: Specialist, case: EvalCase, workspace: Path) -> None:
    fixtures = evals_dir(spec) / "fixtures"
    workspace.mkdir(parents=True, exist_ok=True)
    if case.fixture:
        src = (fixtures / case.fixture).resolve()
        if not src.is_relative_to(fixtures.resolve()) or not src.is_dir():
            raise AgentKitError(f"fixture directory not found: {case.fixture}")
        as_workspace = (src / "inputs").is_dir() or (src / "repo").is_dir()
        shutil.copytree(src, workspace if as_workspace else workspace / "inputs", dirs_exist_ok=True)
    for dest, src_rel in case.fixtures.items():
        src = (fixtures / src_rel).resolve()
        target = (workspace / dest).resolve()
        if not src.is_relative_to(fixtures.resolve()) or not target.is_relative_to(workspace.resolve()):
            raise AgentKitError(f"fixture path escapes its directory: {dest} <- {src_rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)


def run_case(spec: Specialist, case: EvalCase, *, adapter: Any, workspace: Path, grader: Any = None,
             events: EventSink | None = None) -> dict[str, Any]:
    prepare_workspace(spec, case, workspace)
    ctx = RunContext(brief=case_brief(spec, case), workspace=workspace, adapter=adapter, grader=grader,
                     events=events or NullSink())
    sub = spec.run_milestone(ctx, case.milestone)
    return {"case": case.name, "milestone": case.milestone, "status": sub.status,
            "checks": [{"check": r.check, "kind": r.kind, "passed": r.passed, "score": r.score}
                       for r in sub.check_results],
            "usage": sub.usage.to_dict(), "models": list(sub.models),
            "evidence_hash": sub.evidence_hash, "workspace": str(workspace)}


def run_evals(spec: Specialist, *, adapter: Any, out_dir: Path, grader: Any = None,
              only: str | None = None, events: EventSink | None = None) -> list[dict[str, Any]]:
    results = []
    for case in load_cases(spec):
        if only and case.name != only:
            continue
        results.append(run_case(spec, case, adapter=adapter, grader=grader, events=events,
                                workspace=Path(out_dir) / case.name))
    return results


__all__ = ["EvalCase", "case_brief", "load_cases", "parse_case", "prepare_workspace", "run_case",
           "run_evals"]
