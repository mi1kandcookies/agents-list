"""
specialists/test_coverage/agent.py - the test-coverage Specialist.

agent.yaml, TOOL_DEFS (tools.py) and CHECK_DEFS (checks.py) beside this
module are wired in by the kit. This subclass adds what the harness, not the
model, must own in a code engagement: the patch the customer receives.

    prepare   records the commit repo/ is at when the milestone starts, once
              (a resumed run keeps the first one), under .agentkit/ where the
              agent's tools cannot write
    finalize  rebuilds every repo.patch deliverable from `git diff <start>`
              over repo/ (new files included) and overwrites whatever the
              model wrote there, so diff_test_paths_only, patch_size_max and
              the other patch checks grader the real change, committed or not,
              instead of a hand-made patch. If the rebuild fails the
              unverifiable patch is removed, so those checks fail closed.

The rebuild needs repo/ to be a git checkout with at least one commit; when
it is not, a patch_base event says why and the patch is checked as written.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from agentkit.errors import AgentKitError
from agentkit.journal import safe_name
from agentkit.loop import RunOutcome
from agentkit.policy import INTERNAL_DIR
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec
from specialists.test_coverage.tools import export_patch

REPO_DIR = "repo"
PATCH_NAME = "repo.patch"
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")   # SHA-1 or SHA-256 object name


class CoverageSpecialist(Specialist):
    """Test Coverage & Characterization Engineer: test-only patches, rebuilt by the harness."""

    @staticmethod
    def patch_deliverables(milestone: MilestoneSpec) -> list[str]:
        return [d for d in milestone.deliverables
                if PurePosixPath(d.replace("\\", "/")).name == PATCH_NAME]

    def start_file(self, workspace: Path, milestone_id: str) -> Path:
        """Where prepare() keeps the commit the milestone started at."""
        return Path(workspace) / INTERNAL_DIR / self.slug / f"{safe_name(milestone_id)}.start"

    def start_commit(self, workspace: Path, milestone_id: str) -> str | None:
        path = self.start_file(workspace, milestone_id)
        if not path.is_file():
            return None
        sha = path.read_text(encoding="utf-8").strip()
        return sha if _COMMIT.match(sha) else None

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        super().prepare(ctx, milestone)
        if not self.patch_deliverables(milestone) or self.start_commit(ctx.workspace, milestone.id):
            return
        sha, reason = self._head(ctx)
        if sha is None:
            ctx.events.emit("patch_base", milestone=milestone.id, commit=None, reason=reason)
            return
        path = self.start_file(ctx.workspace, milestone.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sha + "\n", encoding="utf-8")
        ctx.events.emit("patch_base", milestone=milestone.id, commit=sha)

    def finalize(self, ctx: ToolContext, milestone: MilestoneSpec, outcome: RunOutcome) -> RunOutcome:
        outcome = super().finalize(ctx, milestone, outcome)
        start = self.start_commit(ctx.workspace, milestone.id)
        if start is None:
            return outcome
        for rel in self.patch_deliverables(milestone):
            target = ctx.path(rel, write=True)
            written = target.read_bytes() if target.is_file() else None
            try:
                if not (ctx.workspace / REPO_DIR / ".git").exists():
                    # git would search the parent directories for a repository
                    raise AgentKitError("repo/ is no longer a git checkout")
                report = export_patch(ctx.workspace, run=ctx.run, resolve_path=ctx.path,
                                      out=rel, base=start)
            except (AgentKitError, OSError) as exc:
                target.unlink(missing_ok=True)
                ctx.events.emit("patch_rebuilt", milestone=milestone.id, path=rel, ok=False,
                                error=str(exc))
                continue
            model_patch = ("missing" if written is None
                           else "same" if written == target.read_bytes() else "replaced")
            ctx.events.emit("patch_rebuilt", milestone=milestone.id, path=rel, ok=True, commit=start,
                            model_patch=model_patch, files_changed=report["files_changed"],
                            outside_test_paths=report["outside_test_paths"])
        return outcome

    @staticmethod
    def _head(ctx: ToolContext) -> tuple[str | None, str]:
        """(commit, "") for repo/'s HEAD, or (None, why not)."""
        repo = ctx.workspace / REPO_DIR
        if not (repo / ".git").exists():   # never a parent directory's repository
            return None, "repo/ is not a git checkout"
        try:
            res = ctx.run(["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=REPO_DIR)
        except AgentKitError as exc:       # git missing or not allowlisted
            return None, str(exc)
        sha = res.stdout.strip()
        if res.exit_code != 0 or not _COMMIT.match(sha):
            return None, "repo/ has no commit to diff against"
        return sha, ""
