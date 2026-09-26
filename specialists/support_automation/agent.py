"""
specialists/support_automation/agent.py - the support-automation Specialist.

agent.yaml, prompts/, playbook/, rubrics/, tools.py (TOOL_DEFS) and
checks.py (CHECK_DEFS) beside this file are picked up by the kit; the
subclass adds what this domain needs on top:

    validate_intake     a regulated vertical (anything but "no") needs a
                        named compliance approver before work starts
    propose_milestones  the intake's top_n_gaps raises how many knowledge
                        gaps m2-knowledge must fill (the manifest's 5 stays
                        the floor); the intake's extra must-escalate
                        categories become required escalation rules in
                        m3-agent-config, next to the five defaults
    prepare             creates directory deliverables (articles/)
    finalize            lists every file in a directory deliverable as an
                        artifact, so each article is hashed into the
                        evidence even if the model did not list it
    check_registry      rubric_grader and no_placeholders accept a directory
                        in `paths` (the articles folder) and check the files
                        in it

Nothing here publishes, sends or activates anything, and the human gate
comes from the manifest unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from agentkit.checks import CheckContext, CheckRegistry
from agentkit.checks.builtin import no_placeholders, rubric_grader
from agentkit.errors import PolicyViolation
from agentkit.loop import RunOutcome
from agentkit.policy import jail_path
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import CheckResult, MilestoneSpec, MissingInput
from specialists.support_automation.checks import REQUIRED_ESCALATIONS

GAP_MILESTONE = "m2-knowledge"
GAP_CHECK = "top_gaps_addressed"
CONFIG_MILESTONE = "m3-agent-config"
CONFIG_CHECK = "agent_config_valid"
# File types the grader reads when a rubric names a directory.
JUDGED_SUFFIXES = (".md", ".json", ".csv", ".txt")
# Answers to "regulated vertical?" that mean no.
NOT_REGULATED = {"", "no", "none", "n/a", "na", "false", "0", "not regulated", "nope"}
# Answers to "extra must-escalate categories?" that mean none.
NO_CATEGORIES = {"", "none", "no", "n_a", "na", "nothing"}


def _files_in(directory: Path) -> list[Path]:
    """Regular files directly inside a directory, sorted; links are skipped."""
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.is_symlink())


def expand_dirs(workspace: Path, paths: list[str], suffixes: tuple[str, ...] | None = JUDGED_SUFFIXES) -> list[str]:
    """Workspace paths with each directory replaced by the files in it (only
    `suffixes`, or every file with None)."""
    out: list[str] = []
    for rel in paths:
        target = jail_path(Path(workspace), rel)
        if not target.is_dir():
            out.append(rel)
            continue
        base = rel.replace("\\", "/").rstrip("/")
        out += [f"{base}/{p.name}" for p in _files_in(target)
                if suffixes is None or p.suffix.lower() in suffixes]
    return out


def _given_paths(params: dict) -> list[str]:
    return list(params.get("paths") or ([params["path"]] if params.get("path") else []))


def rubric_grader_dirs(workspace: Path, params: dict, ctx: CheckContext) -> CheckResult:
    """The kit's rubric_grader, with a directory in `paths` standing for the
    files directly inside it. Pending (None) without a grader, as before."""
    if ctx.grader is None:
        return rubric_grader(workspace, params, ctx)
    paths = _given_paths(params)
    files = expand_dirs(workspace, paths)
    if not files:
        return CheckResult(check="", passed=False, details=f"nothing to grader in {', '.join(paths)}")
    return rubric_grader(workspace, {**params, "paths": files}, ctx)


def no_placeholders_dirs(workspace: Path, params: dict, ctx: CheckContext) -> CheckResult:
    """The kit's no_placeholders, with a directory in `paths` standing for
    every file directly inside it (an empty folder has nothing to flag)."""
    paths = _given_paths(params)
    if not paths:
        return no_placeholders(workspace, params, ctx)          # the kit's "no path given" failure
    files = expand_dirs(workspace, paths, suffixes=None)
    if not files:
        return CheckResult(check="", passed=True, details="no files to check")
    return no_placeholders(workspace, {k: v for k, v in params.items() if k != "path"} | {"paths": files}, ctx)


def _top_n(intake: Mapping[str, Any]) -> int | None:
    value = intake.get("top_n_gaps")
    if isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def category_id(text: str) -> str:
    """'Food illness or allergic reaction' -> 'food_illness_or_allergic_reaction'."""
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60]


def extra_categories(intake: Mapping[str, Any]) -> list[str]:
    """The intake's must-escalate categories beyond the defaults, as rule
    category ids (a list, or text separated by commas, semicolons or lines)."""
    value = intake.get("must_escalate_categories")
    items = value if isinstance(value, list) else re.split(r"[,;\n]", str(value or ""))
    out: list[str] = []
    for item in items:
        cid = category_id(item)
        if cid not in NO_CATEGORIES and cid not in REQUIRED_ESCALATIONS and cid not in out:
            out.append(cid)
    return out


def is_regulated(value: Any) -> bool:
    """True unless the regulated_vertical answer says no."""
    if value is None or value is False:
        return False
    text = " ".join(str(value).lower().split())
    return not (text in NOT_REGULATED or re.match(r"^(no|not|none)\b", text))


class SupportAutomation(Specialist):

    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        if is_regulated(intake.get("regulated_vertical")) and not str(intake.get("compliance_approver") or "").strip():
            missing = [m for m in missing if m.field != "compliance_approver"]
            missing.append(MissingInput(
                field="compliance_approver", blocking=True,
                question="You told us you operate in a regulated vertical. Name the person (and role) who "
                         "approves escalation rules, AI disclosures and regulated wording before anything "
                         "goes live; work cannot start without one."))
        return missing

    def propose_milestones(self, intake: Mapping[str, Any] | None = None) -> list[MilestoneSpec]:
        milestones = super().propose_milestones(intake)
        intake = dict(intake or {})
        n = _top_n(intake)
        extra = extra_categories(intake)
        for m in milestones:
            for a in m.acceptance:
                if m.id == GAP_MILESTONE and a.check == GAP_CHECK and n is not None:
                    a.params["top_n"] = max(n, int(a.params.get("top_n", 5)))
                if m.id == CONFIG_MILESTONE and a.check == CONFIG_CHECK and extra:
                    cats = [*(a.params.get("required_escalations") or REQUIRED_ESCALATIONS), *extra]
                    a.params["required_escalations"] = list(dict.fromkeys(cats))
                    a.description = ("Config has instructions, AI disclosure, handoff and escalation rules "
                                     "for categories: " + ", ".join(a.params["required_escalations"]))
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
        reg.register("no_placeholders", no_placeholders_dirs, kind="automated", replace=True)
        return reg
