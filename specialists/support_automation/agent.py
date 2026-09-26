"""
specialists/support_automation/agent.py - the support-automation Specialist.

agent.yaml, prompts/, playbook/, rubrics/, tools.py (TOOL_DEFS) and
checks.py (CHECK_DEFS) beside this file are picked up by the kit; the
subclass adds what this domain needs on top:

    propose_milestones  the intake's top_n_gaps raises how many knowledge
                        gaps m2-knowledge must fill (the manifest's 5 stays
                        the floor)
    prepare             creates directory deliverables (articles/)
    finalize            lists every file in a directory deliverable as an
                        artifact, so each article is hashed into the
                        evidence even if the model did not list it
    check_registry      rubric_grader accepts a directory in `paths` (the
                        articles folder) and grades the files in it

Nothing here publishes, sends or activates anything, and the human gate
comes from the manifest unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from agentkit.checks import CheckContext, CheckRegistry
from agentkit.checks.builtin import rubric_grader
from agentkit.errors import PolicyViolation
from agentkit.loop import RunOutcome
from agentkit.policy import jail_path
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import CheckResult, MilestoneSpec

GAP_MILESTONE = "m2-knowledge"
GAP_CHECK = "top_gaps_addressed"
# File types the grader reads when a rubric names a directory.
JUDGED_SUFFIXES = (".md", ".json", ".csv", ".txt")


def _files_in(directory: Path) -> list[Path]:
    """Regular files directly inside a directory, sorted; links are skipped."""
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.is_symlink())


def expand_dirs(workspace: Path, paths: list[str]) -> list[str]:
    """Workspace paths with each directory replaced by the judged files in it."""
    out: list[str] = []
    for rel in paths:
        target = jail_path(Path(workspace), rel)
        if not target.is_dir():
            out.append(rel)
            continue
        base = rel.replace("\\", "/").rstrip("/")
        out += [f"{base}/{p.name}" for p in _files_in(target) if p.suffix.lower() in JUDGED_SUFFIXES]
    return out


def rubric_grader_dirs(workspace: Path, params: dict, ctx: CheckContext) -> CheckResult:
    """The kit's rubric_grader, with a directory in `paths` standing for the
    files directly inside it. Pending (None) without a grader, as before."""
    if ctx.grader is None:
        return rubric_grader(workspace, params, ctx)
    paths = params.get("paths") or ([params["path"]] if params.get("path") else [])
    files = expand_dirs(workspace, list(paths))
    if not files:
        return CheckResult(check="", passed=False, details=f"nothing to grader in {', '.join(paths)}")
    return rubric_grader(workspace, {**params, "paths": files}, ctx)


def _top_n(intake: Mapping[str, Any]) -> int | None:
    value = intake.get("top_n_gaps")
    if isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


class SupportAutomation(Specialist):

    def propose_milestones(self, intake: Mapping[str, Any] | None = None) -> list[MilestoneSpec]:
        milestones = super().propose_milestones(intake)
        n = _top_n(dict(intake or {}))
        if n is None:
            return milestones
        for m in milestones:
            if m.id != GAP_MILESTONE:
                continue
            for a in m.acceptance:
                if a.check == GAP_CHECK:
                    a.params["top_n"] = max(n, int(a.params.get("top_n", 5)))
        return milestones

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        super().prepare(ctx, milestone)
        for d in milestone.deliverables:
            if d.endswith("/"):
                ctx.path(d, write=True).mkdir(parents=True, exist_ok=True)

    def finalize(self, ctx: ToolContext, milestone: MilestoneSpec, outcome: RunOutcome) -> RunOutcome:
        outcome = super().finalize(ctx, milestone, outcome)
        listed = list(outcome.artifacts)
        for d in milestone.deliverables:
            try:
                directory = ctx.path(d)
            except PolicyViolation:
                continue
            if directory.is_dir():
                listed += [ctx.rel(p) for p in _files_in(directory)]
        outcome.artifacts = list(dict.fromkeys(listed))
        return outcome

    def check_registry(self) -> CheckRegistry:
        reg = super().check_registry()
        reg.register("rubric_grader", rubric_grader_dirs, kind="rubric", replace=True)
        return reg
