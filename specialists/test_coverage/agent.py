"""
specialists/test_coverage/agent.py - the test-coverage Specialist.

agent.yaml, TOOL_DEFS (tools.py) and CHECK_DEFS (checks.py) beside this
module are wired in by the kit. This subclass adds what the harness, not the
model, must own in a code engagement: the patch the customer receives and
the runs that show it is stable.

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
              Then, for each tests_stable criterion, it re-runs the command
              the model last gave run_test_matrix (recorded in matrix.json)
              at least min_runs times against repo/ as delivered, replacing
              the model's run files; without a usable command they are
              removed. Last, it records the test ids a submitted
              milestone's runs showed, which later milestones' tests_stable
              compares against (params.baseline).

repo/ does not need to be a git checkout: the store reads it as a plain
work tree.
"""
from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path, PurePosixPath
from typing import Any

from agentkit.errors import AgentKitError
from agentkit.loop import RunOutcome
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec
from specialists.test_coverage.snapshot import REPO_DIR, RepoStore
from specialists.test_coverage.tools import (_glob_files, census_from_runs, matrix_dir, parse_junit_text,
                                              parse_patch, run_test_matrix, runs_inline_code, scope_report)

PATCH_NAME = "repo.patch"
MAX_DRIFT_LISTED = 50
MAX_RUNS = 30   # run_test_matrix's own cap


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
        submitted = outcome.status == "submitted"
        if delivered and submitted:
            entry["delivered"] = delivered
            state.setdefault("milestones", {})[milestone.id] = entry
            state["delivered_order"] = [m for m in state.get("delivered_order", [])
                                        if m != milestone.id] + [milestone.id]
        self._replay_runs(ctx, milestone, submitted)
        if submitted:
            self._record_suite(ctx, milestone, state)
        state["active"] = None
        store.save(state)
        return outcome

    # --- runs ----------------------------------------------------------------------

    def _replay_runs(self, ctx: ToolContext, milestone: MilestoneSpec, submitted: bool) -> None:
        """For each tests_stable criterion over run_test_matrix's layout,
        re-run the command the model last used there, at least min_runs
        times, against repo/ as delivered. The run files the checks read are
        then the harness's; without a usable command they are removed."""
        done: set[str] = set()
        for crit in milestone.acceptance:
            runs_dir = matrix_dir(crit.params.get("runs")) if crit.check == "tests_stable" else None
            if runs_dir is None or runs_dir in done:
                continue
            done.add(runs_dir)
            try:
                if not submitted:
                    raise AgentKitError("the milestone was not submitted")
                cmd = self._matrix_command(ctx, runs_dir)
                runs = min(MAX_RUNS, max(int(crit.params.get("min_runs", 10)), cmd["runs"]))
                census = run_test_matrix(ctx.workspace, run=ctx.run, resolve_path=ctx.path, argv=cmd["argv"],
                                         runs=runs, runs_dir=runs_dir, cwd=cmd["cwd"],
                                         base_seed=cmd["base_seed"], timeout=cmd["timeout"])
                record = ctx.path(f"{runs_dir}/matrix.json", write=True)
                data = json.loads(record.read_text(encoding="utf-8"))
                data["replayed_by"] = "harness"
                record.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            except (AgentKitError, OSError, ValueError, TypeError) as exc:
                self._clear_runs(ctx, runs_dir)
                ctx.events.emit("matrix_replayed", milestone=milestone.id, runs_dir=runs_dir, ok=False,
                                error=str(exc))
            else:
                ctx.events.emit("matrix_replayed", milestone=milestone.id, runs_dir=runs_dir, ok=True,
                                argv=cmd["argv"], runs=census["runs"], tests=census["tests"],
                                all_green=census["all_green"])

    def _matrix_command(self, ctx: ToolContext, runs_dir: str) -> dict[str, Any]:
        """The command run_test_matrix recorded in runs_dir/matrix.json, checked."""
        self._own_path(ctx, runs_dir)
        data = json.loads(self._own_path(ctx, f"{runs_dir}/matrix.json").read_text(encoding="utf-8"))
        cmd = data.get("command") if isinstance(data, dict) else None
        if not isinstance(cmd, dict):
            raise AgentKitError("matrix.json records no command (run_test_matrix writes one)")
        argv = cmd.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise AgentKitError("matrix.json: command.argv must be a list of strings")
        if not any("{junit}" in a for a in argv):
            raise AgentKitError("the command writes no JUnit file ({junit})")
        if runs_inline_code(argv):
            raise AgentKitError("the command runs inline code instead of the test suite")
        cwd = posixpath.normpath(str(cmd.get("cwd") or REPO_DIR).replace("\\", "/"))
        if cwd != REPO_DIR and not cwd.startswith(REPO_DIR + "/"):
            raise AgentKitError("the command must run inside repo/")
        timeout = cmd.get("timeout")
        return {"argv": argv, "cwd": cwd, "runs": int(cmd.get("runs") or 0),
                "base_seed": int(cmd.get("base_seed") or 1000),
                "timeout": None if timeout is None else int(timeout)}

    def _clear_runs(self, ctx: ToolContext, runs_dir: str) -> None:
        """Remove run files the harness did not make (the link itself, if runs_dir is one)."""
        try:
            resolved = ctx.path(runs_dir, write=True)
            link = self._lexical(ctx, runs_dir)
            if link.is_symlink():
                link.unlink()
                return
            for p in resolved.glob("run-*.xml"):
                if p.is_symlink() or p.is_file():
                    p.unlink()
        except (AgentKitError, OSError):
            pass

    def _record_suite(self, ctx: ToolContext, milestone: MilestoneSpec, state: dict[str, Any]) -> None:
        """Keep the tests this milestone's runs showed (and which were flaky or
        broken) for later milestones' tests_stable (params.baseline)."""
        for crit in milestone.acceptance:
            if crit.check not in ("tests_stable", "flake_census_matches") or not crit.params.get("runs"):
                continue
            try:
                files = _glob_files(ctx.workspace, str(crit.params["runs"]), ctx.path)
                runs = [parse_junit_text(p.read_text(encoding="utf-8", errors="replace"), p.name)
                        for p in files]
            except (AgentKitError, OSError):
                continue
            if not runs:
                continue
            census = census_from_runs(runs)
            ids = sorted(set.intersection(*(set(r) for r in runs)))
            state.setdefault("suites", {})[milestone.id] = {
                "runs": len(runs), "ids": ids, "flaky": census["flaky"], "broken": census["broken"],
                "skipped": census["skipped"]}
            ctx.events.emit("suite_recorded", milestone=milestone.id, runs=len(runs), tests=len(ids))
            return

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
        harness would then overwrite, such as a test in repo/)."""
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
