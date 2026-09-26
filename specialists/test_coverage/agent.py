"""
specialists/test_coverage/agent.py - the test-coverage Specialist.

agent.yaml, TOOL_DEFS (tools.py) and CHECK_DEFS (checks.py) beside this
module are wired in by the kit. This subclass adds what the harness, not the
model, must own in a code engagement: the patch the customer receives.

    prepare   snapshots repo/ into the harness's private store
              (snapshot.RepoStore) the first time any milestone starts, and
              fixes a patch milestone's base the first time it starts (a
              resumed or repeated run keeps it): the tree the last submitted
              patch milestone delivered, else that first snapshot. A
              patch_base event lists any drift of repo/ from that base.
    finalize  rebuilds every repo.patch deliverable as diff(base, repo/ now)
              from the private store and overwrites whatever the model wrote
              there, so diff_test_paths_only, patch_size_max and the other
              patch checks grader the real change, whatever the model did to
              repo/.git. If the rebuild fails the unverifiable patch is
              removed, so those checks fail closed. A submitted milestone's
              tree becomes the next patch milestone's base.

repo/ does not need to be a git checkout: the store reads it as a plain
work tree.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from agentkit.errors import AgentKitError
from agentkit.loop import RunOutcome
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec
from specialists.test_coverage.snapshot import RepoStore
from specialists.test_coverage.tools import parse_patch, scope_report

PATCH_NAME = "repo.patch"
MAX_DRIFT_LISTED = 50


class CoverageSpecialist(Specialist):
    """Test Coverage & Characterization Engineer: test-only patches, rebuilt by the harness."""

    @staticmethod
    def patch_deliverables(milestone: MilestoneSpec) -> list[str]:
        return [d for d in milestone.deliverables
                if PurePosixPath(d.replace("\\", "/")).name == PATCH_NAME]

    @staticmethod
    def store(ctx: ToolContext) -> RepoStore:
        return RepoStore(ctx.workspace, timeout=ctx.policy.shell_timeout)

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        super().prepare(ctx, milestone)
        store = self.store(ctx)
        state = store.state()
        if not state.get("origin"):
            try:
                state["origin"] = store.tree(index="index.origin")
                ctx.events.emit("repo_snapshot", milestone=milestone.id, ok=True, tree=state["origin"])
            except AgentKitError as exc:
                ctx.events.emit("repo_snapshot", milestone=milestone.id, ok=False, error=str(exc))
        state["active"] = None
        if self.patch_deliverables(milestone):
            entry = state.setdefault("milestones", {}).setdefault(milestone.id, {})
            if not entry.get("base") and state.get("origin"):
                earlier = [m for m in state.get("delivered_order", []) if m != milestone.id]
                entry["base_from"] = earlier[-1] if earlier else "origin"
                entry["base"] = (state["milestones"][earlier[-1]]["delivered"] if earlier
                                 else state["origin"])
            if entry.get("base"):
                state["active"] = {"milestone": milestone.id, "base": entry["base"]}
                ctx.events.emit("patch_base", milestone=milestone.id, base=entry["base"],
                                base_from=entry["base_from"], drift=self._drift(store, entry["base"]))
            else:
                ctx.events.emit("patch_base", milestone=milestone.id, base=None,
                                reason="repo/ could not be snapshotted")
        store.save(state)

    def finalize(self, ctx: ToolContext, milestone: MilestoneSpec, outcome: RunOutcome) -> RunOutcome:
        outcome = super().finalize(ctx, milestone, outcome)
        store = self.store(ctx)
        state = store.state()
        entry = (state.get("milestones") or {}).get(milestone.id) or {}
        delivered = None
        for rel in self.patch_deliverables(milestone):
            try:
                target = self._own_path(ctx, rel)
                written = target.read_bytes() if target.is_file() else None
                if not entry.get("base"):
                    raise AgentKitError("no base was recorded when the milestone started")
                delivered = delivered or store.tree(entry["base"])
                store.write_patch(entry["base"], delivered, target)
                report = scope_report(parse_patch(target.read_text(encoding="utf-8", errors="replace")))
            except (AgentKitError, OSError) as exc:
                self._withdraw(ctx, rel)
                ctx.events.emit("patch_rebuilt", milestone=milestone.id, path=rel, ok=False, error=str(exc))
                continue
            model_patch = ("missing" if written is None
                           else "same" if written == target.read_bytes() else "replaced")
            ctx.events.emit("patch_rebuilt", milestone=milestone.id, path=rel, ok=True,
                            base=entry["base"], base_from=entry.get("base_from"), tree=delivered,
                            model_patch=model_patch, files_changed=report["files_changed"],
                            outside_test_paths=report["outside_test_paths"])
        if delivered and outcome.status == "submitted":
            entry["delivered"] = delivered
            state.setdefault("milestones", {})[milestone.id] = entry
            state["delivered_order"] = [m for m in state.get("delivered_order", [])
                                        if m != milestone.id] + [milestone.id]
        state["active"] = None
        store.save(state)
        return outcome

    # --- helpers -------------------------------------------------------------------

    @staticmethod
    def _drift(store: RepoStore, base: str) -> list[str] | None:
        """Paths where repo/ differs from the base right now (None if unknown)."""
        try:
            changed = store.changes(base, store.tree(base, index="index.drift"))
        except AgentKitError:
            return None
        return [c["path"] for c in changed][:MAX_DRIFT_LISTED]

    @staticmethod
    def _lexical(ctx: ToolContext, rel: str) -> Path:
        return Path(os.path.normpath(ctx.workspace / rel))

    def _own_path(self, ctx: ToolContext, rel: str) -> Path:
        """A deliverable path the harness writes: inside the policy's rules
        and reached without a link (the model could point one at a file the
        harness would then overwrite)."""
        target = ctx.path(rel, write=True)
        if target != self._lexical(ctx, rel):
            raise AgentKitError(f"{rel} is reached through a link")
        return target

    def _withdraw(self, ctx: ToolContext, rel: str) -> None:
        """Remove an unverifiable deliverable (the link itself, if it is one)."""
        path = self._lexical(ctx, rel)
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
        except OSError:
            pass
