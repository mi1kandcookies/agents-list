"""
specialists/test_coverage/snapshot.py - the harness's private record of
repo/, from which every delivered repo.patch is built.

The customer receives a patch, so the harness, not the model, decides what
it holds. RepoStore keeps its own git object store in
.agentkit/test-coverage/store.git (the agent's tools cannot write there) and
reads repo/ only as a work tree:

    origin      the tree of repo/ when the first milestone started
    base        per patch milestone, fixed when it first starts: the tree
                the last submitted patch milestone delivered, else origin
    delivered   per patch milestone, recorded when it submits: the tree of
                repo/ at the end; repo.patch = diff(base, delivered)

So a later milestone's patch never repeats an earlier one's tests, and
production edits made in any earlier milestone surface in the next patch.

repo/.git plays no part. Its index, config, attributes and refs are the
model's to change (skip-worktree bits, diff prefixes, filters, replace refs),
so every command here names the private store with --git-dir, keeps its
index there, and runs with an isolated configuration: no system or global
config, an empty HOME, a harness-written info/exclude. repo/'s own .gitignore
files still leave out new files the customer's checkout would ignore, as
does TOOL_OUTPUT (caches, coverage data, virtualenvs); files the base
tracks are always compared, ignored or not.

git runs with the harness's privileges (it writes .agentkit/), so repo/ must
be a real directory at its place in the workspace, never a link elsewhere.
state.json beside the store holds the trees above, the active milestone's
base (read by the export_patch preview) and the test ids each milestone's
runs showed (read by tests_stable).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agentkit.errors import AgentKitError
from agentkit.journal import write_json_atomic
from agentkit.policy import INTERNAL_DIR

STATE_DIR = f"{INTERNAL_DIR}/test-coverage"
REPO_DIR = "repo"
GITLINK = "160000"
_OBJECT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")   # SHA-1 or SHA-256 object name

# New files matching these are tool output and never part of a patch
# (info/exclude syntax: a leading "/" anchors at repo/'s root).
TOOL_OUTPUT = [
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".coverage", ".coverage.*", "/coverage.xml",
    "htmlcov/", ".mutmut-cache", "/mutants/", ".hypothesis/", ".tox/", ".nox/", ".mypy_cache/",
    ".ruff_cache/", "*.egg-info/", ".eggs/", ".venv/", "/venv/", "node_modules/", ".nyc_output/",
    "/coverage/", "/lcov.info", "/coverage.out", "/cover.out", "/build/", "/dist/", "/target/",
    ".gradle/",
]
# Environment variables git keeps from the harness; everything else, notably
# GIT_* and XDG_*, is dropped.
_ENV_KEYS = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR",
             "LANG", "LC_ALL", "TZ")
_CONFIG = ["-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
           "-c", "core.untrackedCache=false", "-c", "core.quotePath=false",
           "-c", "advice.addEmbeddedRepo=false"]


class RepoStore:
    def __init__(self, workspace: Path, *, timeout: float = 600.0, git: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / STATE_DIR
        self.git_dir = self.root / "store.git"
        self.state_path = self.root / "state.json"
        self.timeout = float(timeout)
        self._git_exe = git

    # --- state -----------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, state)

    def active_base(self) -> str | None:
        """The base tree of the patch milestone that is running, if any."""
        base = (self.state().get("active") or {}).get("base")
        return base if isinstance(base, str) and _OBJECT.match(base) else None

    # --- git -------------------------------------------------------------------

    def repo(self) -> Path:
        """repo/ as a real directory at its place in the workspace."""
        path = self.workspace / REPO_DIR
        if path.is_symlink() or path.is_junction():
            raise AgentKitError("repo/ is a link, not a directory")
        if not path.is_dir():
            raise AgentKitError("repo/ is missing")
        if path.resolve() != path:
            raise AgentKitError("repo/ does not resolve to its own place in the workspace")
        return path

    def _exe(self) -> str:
        found = self._git_exe or shutil.which("git", path=os.environ.get("PATH", ""))
        if not found:
            raise AgentKitError("git is not installed")
        real = Path(found).resolve()
        if real == self.workspace or real.is_relative_to(self.workspace):
            raise AgentKitError("git resolves to a program inside the workspace")
        return found

    def _env(self, index: str | None) -> dict[str, str]:
        env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"),
                   GIT_ATTR_NOSYSTEM="1", HOME=str(self.root / "home"), GIT_TERMINAL_PROMPT="0",
                   GIT_OPTIONAL_LOCKS="0")
        if index:
            env["GIT_INDEX_FILE"] = str(self.git_dir / index)
        return env

    def _run(self, argv: list[str], *, index: str | None = None, cwd: Path | None = None) -> bytes:
        try:
            res = subprocess.run(argv, cwd=cwd or self.root, env=self._env(index), stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            raise AgentKitError(f"git {argv[1 if len(argv) > 1 else 0]} timed out") from None
        except OSError as exc:
            raise AgentKitError(f"git could not run: {exc}") from None
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip()
            raise AgentKitError(f"git failed ({res.returncode}): {err[:500]}")
        return res.stdout

    def git(self, *args: str, index: str | None = None, worktree: bool = False) -> bytes:
        argv = [self._exe(), f"--git-dir={self.git_dir}"]
        cwd = None
        if worktree:
            cwd = self.repo()
            argv.append(f"--work-tree={cwd}")
        return self._run([*argv, *_CONFIG, *args], index=index, cwd=cwd)

    def ensure(self) -> None:
        """Create the private store (idempotent)."""
        if (self.git_dir / "HEAD").is_file():
            return
        (self.root / "home").mkdir(parents=True, exist_ok=True)
        (self.root / "gitconfig").write_text("", encoding="utf-8")
        self._run([self._exe(), "init", "-q", "--bare", "--template=", str(self.git_dir)])
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text("\n".join(TOOL_OUTPUT) + "\n", encoding="utf-8")

    def tree(self, base: str | None = None, *, index: str = "index") -> str:
        """The tree of repo/ as it is now. With `base`, every file the base
        tracks is compared (even if repo/'s ignore rules now cover it)."""
        self.ensure()
        self.git("read-tree", base or "--empty", index=index)
        self.git("add", "-A", index=index, worktree=True)
        sha = self.git("write-tree", index=index).decode("ascii", errors="replace").strip()
        if not _OBJECT.match(sha):
            raise AgentKitError("git write-tree returned no tree")
        return sha

    def changes(self, a: str, b: str) -> list[dict[str, str]]:
        """Paths that differ between two trees, with their modes and status."""
        raw = self.git("diff-tree", "-r", "-z", "--raw", "--no-renames", a, b)
        parts = raw.decode("utf-8", errors="replace").split("\0")
        out = []
        for meta, path in zip(parts[0::2], parts[1::2]):
            fields = meta.lstrip(":").split()
            if len(fields) >= 5:
                out.append({"old_mode": fields[0], "new_mode": fields[1], "status": fields[4][:1],
                            "path": path})
        return out

    def write_patch(self, a: str, b: str, target: Path) -> list[dict[str, str]]:
        """Write diff(a, b) to `target` as a patch `git apply` takes (binary
        files included). Refuses a change to an embedded repository, whose
        files a patch cannot carry. Returns the changed paths."""
        changed = self.changes(a, b)
        links = [c["path"] for c in changed if GITLINK in (c["old_mode"], c["new_mode"])]
        if links:
            raise AgentKitError(f"embedded git repository in repo/ at {links[:5]}: "
                                "its files cannot be delivered in a patch")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.git("diff-tree", "-r", "-p", "--binary", "--full-index", "--no-renames", "--no-color",
                 "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
                 f"--output={target}", a, b)
        if not target.is_file():
            target.write_bytes(b"")
        return changed


__all__ = ["REPO_DIR", "RepoStore", "STATE_DIR", "TOOL_OUTPUT"]
