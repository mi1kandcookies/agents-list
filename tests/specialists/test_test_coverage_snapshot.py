"""test-coverage specialist: the harness's private repo store (snapshot.py)
and the export_patch preview built on it. Real git on a tmp workspace."""
import shutil
import subprocess
from pathlib import Path

import pytest

from agentkit.errors import AgentKitError, ToolError
from specialists.test_coverage import tools as T
from specialists.test_coverage.snapshot import RepoStore

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="the store runs git")


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(repo: Path, *args: str) -> str:
    cfg = ["-c", "user.name=Dev", "-c", "user.email=dev@example.invalid", "-c", "commit.gpgsign=false",
           "-c", "core.hooksPath=.git/no-hooks", "-c", "core.autocrlf=false"]
    return subprocess.run([GIT, *cfg, *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def ws(tmp_path):
    ws = tmp_path / "ws"
    write(ws, "repo/pkg/calc.py", "def add(a, b):\n    return a + b\n")
    write(ws, "repo/tests/test_calc.py", "from pkg.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    write(ws, "repo/.gitignore", "*.log\n")
    return ws


def activate(store: RepoStore, base: str) -> None:
    state = store.state()
    state["active"] = {"milestone": "m2", "base": base}
    store.save(state)


def test_store_diffs_the_work_tree_without_repo_git(ws):
    """repo/ need not be a checkout; new files count, ignored ones and tool output do not."""
    store = RepoStore(ws)
    base = store.tree()
    write(ws, "repo/tests/test_more.py", "def test_more():\n    assert 2 > 1\n")
    write(ws, "repo/pkg/calc.py", "def add(a, b):\n    return a + b + 0\n")
    write(ws, "repo/run.log", "noise\n")                       # repo/.gitignore
    write(ws, "repo/.coverage", "data")                          # tool output
    write(ws, "repo/coverage.xml", "<coverage/>")
    write(ws, "repo/pkg/__pycache__/calc.cpython-312.pyc", "x")
    write(ws, "repo/pkg.egg-info/PKG-INFO", "Name: pkg\n")
    changed = {c["path"]: c["status"] for c in store.changes(base, store.tree(base))}
    assert changed == {"tests/test_more.py": "A", "pkg/calc.py": "M"}
    assert (ws / ".agentkit/test-coverage/store.git/HEAD").is_file()


def test_repo_git_tricks_do_not_reach_the_store(ws):
    """Skip-worktree bits, diff prefixes and exclude files in repo/.git are the
    model's to set; the store never reads them."""
    repo = ws / "repo"
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    store = RepoStore(ws)
    base = store.tree()
    git(repo, "update-index", "--skip-worktree", "pkg/calc.py")
    git(repo, "config", "diff.srcPrefix", "a/tests/")
    git(repo, "config", "diff.dstPrefix", "b/tests/")
    write(ws, "repo/.git/info/exclude", "pkg/\n")
    write(ws, "repo/pkg/calc.py", "def add(a, b):\n    return 3\n")
    write(ws, "repo/pkg/extra.py", "X = 1\n")
    target = ws / "deliverables/m2/repo.patch"
    store.write_patch(base, store.tree(base), target)
    patch = target.read_text(encoding="utf-8")
    assert "diff --git a/pkg/calc.py b/pkg/calc.py" in patch and "+    return 3" in patch
    assert "diff --git a/pkg/extra.py b/pkg/extra.py" in patch
    assert T.scope_report(T.parse_patch(patch))["outside_test_paths"] == ["pkg/calc.py", "pkg/extra.py"]


def test_binary_patch_applies_to_the_base(ws, tmp_path):
    store = RepoStore(ws)
    base = store.tree()
    clone = tmp_path / "clone"                  # the customer's checkout of the base
    shutil.copytree(ws / "repo", clone)
    golden = bytes(range(256)) * 4
    (ws / "repo/tests/golden.bin").write_bytes(golden)
    write(ws, "repo/tests/test_calc.py", "from pkg.calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    target = ws / "deliverables/m2/repo.patch"
    store.write_patch(base, store.tree(base), target)
    assert "GIT binary patch" in target.read_text(encoding="utf-8")
    git(clone, "init", "-q")
    git(clone, "apply", str(target))
    assert (clone / "tests/golden.bin").read_bytes() == golden
    assert "add(2, 2) == 4" in (clone / "tests/test_calc.py").read_text(encoding="utf-8")


def test_embedded_repository_is_refused(ws):
    store = RepoStore(ws)
    base = store.tree()
    sub = ws / "repo/tests/vendored"
    write(sub, "f.txt", "hi\n")
    git(sub, "init", "-q")
    git(sub, "add", "-A")
    git(sub, "commit", "-qm", "inner")
    with pytest.raises(AgentKitError, match="embedded git repository"):
        store.write_patch(base, store.tree(base), ws / "deliverables/m2/repo.patch")
    shutil.rmtree(sub, ignore_errors=True)
    write(ws, "repo/tests/empty/f.txt", "hi\n")
    git(ws / "repo/tests/empty", "init", "-q")               # a nested repository without a commit
    with pytest.raises(AgentKitError):
        store.tree(base)


def test_repo_must_be_a_real_directory(ws, tmp_path):
    store = RepoStore(ws)
    shutil.rmtree(ws / "repo")
    with pytest.raises(AgentKitError, match="missing"):
        store.tree()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    try:
        (ws / "repo").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available here")
    with pytest.raises(AgentKitError, match="link"):
        store.tree()


def test_export_patch_previews_the_active_base(ws):
    with pytest.raises(ToolError, match="no patch base"):
        T.export_patch(ws, out="deliverables/m2/repo.patch")
    store = RepoStore(ws)
    activate(store, store.tree())
    write(ws, "repo/tests/test_new.py", "def test_new():\n    assert 1 < 2\n")
    out = T.export_patch(ws, out="deliverables/m2/repo.patch")
    assert out["test_only"] and out["files_changed"] == 1 and out["written"] == "deliverables/m2/repo.patch"
    assert "+++ b/tests/test_new.py" in (ws / "deliverables/m2/repo.patch").read_text(encoding="utf-8")
    with pytest.raises(Exception):   # the kit's path rules still apply to `out`
        T.export_patch(ws, out=".agentkit/test-coverage/state.json")
