"""
specialists/upgrade_migration/agent.py - the upgrade-migration Specialist.

agent.yaml, prompts, TOOL_DEFS (tools.py) and CHECK_DEFS (checks.py) are
wired by the kit; this subclass adds what a code engagement needs around
the loop:

    extra_checks  every domain check is registered as automated, so a
                  brief cannot turn a recomputation into a pending check
    prepare       records the client's test_command (tests_pass re-runs
                  exactly that), makes sure repo/ is a git repository of its
                  own (a plain copy, such as an eval fixture, gets a baseline
                  commit) and records the commit the milestone starts from
    finalize      writes every repo.patch deliverable from `git diff`
                  against that commit, so the patch the customer merges and
                  the one diff_scope audits is the repo's real change, not
                  a file the model wrote

All git calls go through ToolContext.run (shell allowlist, scrubbed env,
timeout). Nothing is pushed, merged or committed to an existing history.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from agentkit.errors import PolicyViolation, ToolError
from agentkit.journal import safe_name
from agentkit.loop import RunOutcome
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec
from specialists.upgrade_migration import tools as T
from specialists.upgrade_migration.checks import CHECK_DEFS

REPO = "repo"
PATCH_NAME = "repo.patch"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # git's empty tree: diff shows every file
# Identity for the one baseline commit made in a repo/ that arrived without
# git history; signing is off because the VM holds no signing key.
BASELINE_COMMIT = ["-c", "user.name=agentkit", "-c", "user.email=agentkit@localhost.invalid",
                   "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
                   "-m", "agentkit: repo/ as received"]


class UpgradeMigration(Specialist):

    def extra_checks(self) -> list[dict[str, Any]]:
        return [{"name": name, "function": fn, "kind": "automated"} for name, fn in CHECK_DEFS.items()]

    # --- before the loop ----------------------------------------------------------

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        super().prepare(ctx, milestone)
        if ctx.brief is not None:
            T.record_test_command(ctx.workspace, ctx.brief.intake.get("test_command"))
        if not (ctx.workspace / REPO).is_dir() or self.base_path(ctx.workspace, milestone.id).is_file():
            return   # no repository, or a resumed milestone keeps its starting commit
        try:
            base = self._start_commit(ctx)
        except (ToolError, PolicyViolation) as exc:
            ctx.events.emit("warning", milestone=milestone.id, message=f"repo/ baseline not recorded: {exc}")
            return
        if base:
            path = self.base_path(ctx.workspace, milestone.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"base": base, "milestone": milestone.id}) + "\n", encoding="utf-8")
            ctx.events.emit("repo_base", milestone=milestone.id, base=base)

    def _start_commit(self, ctx: ToolContext) -> str | None:
        if not T.is_git_repo(ctx.workspace / REPO):
            for argv in (["git", "init", "-q"], ["git", "add", "--all"], ["git", *BASELINE_COMMIT]):
                res = ctx.run(argv, cwd=REPO)
                if res.exit_code != 0:
                    raise ToolError(f"{' '.join(argv[:3])} failed: {res.stderr.strip()[:300]}")
        res = ctx.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=REPO)
        if res.exit_code == 0 and res.stdout.strip():
            return res.stdout.strip()
        if not res.stderr.strip():
            return EMPTY_TREE          # a repository without commits yet
        raise ToolError(f"git rev-parse failed: {res.stderr.strip()[:300]}")

    @staticmethod
    def base_path(workspace: Path, milestone_id: str) -> Path:
        return Path(workspace) / T.STATE_DIR / "milestones" / f"{safe_name(milestone_id)}.json"

    def base_commit(self, workspace: Path, milestone_id: str) -> str | None:
        path = self.base_path(workspace, milestone_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get("base") or None

    # --- after the loop -------------------------------------------------------------

    def finalize(self, ctx: ToolContext, milestone: MilestoneSpec, outcome: RunOutcome) -> RunOutcome:
        patches = [d for d in milestone.deliverables if PurePosixPath(d).name == PATCH_NAME]
        if not patches or not T.is_git_repo(ctx.workspace / REPO):
            return outcome
        base = self.base_commit(ctx.workspace, milestone.id) or "HEAD"
        staged = ctx.workspace / T.STATE_DIR / "patches" / f"{safe_name(milestone.id)}.patch"
        try:
            res = T.git_diff(ctx.run, REPO, staged, base)
        except (ToolError, PolicyViolation) as exc:
            res = None
            error = str(exc)
        else:
            error = (res.stderr or "").strip()[:300]
        if res is None or res.exit_code != 0 or not staged.is_file():
            ctx.events.emit("warning", milestone=milestone.id, message=f"repo.patch not exported: {error}")
            return outcome
        for rel in patches:
            target = ctx.path(rel, write=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged, target)
            ctx.ledger.note_authored(ctx.rel(target))
        ctx.events.emit("patch_exported", milestone=milestone.id, base=base, paths=patches,
                        bytes=staged.stat().st_size)
        return outcome
