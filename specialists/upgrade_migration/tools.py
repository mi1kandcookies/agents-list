"""
specialists/upgrade_migration/tools.py - deterministic domain tools for the
upgrade-migration specialist.

The model decides what to upgrade and how to adapt call sites; everything that
can be computed is computed here so the numbers in a deliverable come from
code, not from the model:

    inventory_dependencies  parse lockfiles/manifests under repo/ into one list
                            (files of other ecosystems become "unknown" rows)
    osv_scan                query OSV for every inventoried version, snapshot
                            the answers and the full advisory records
    plan_upgrades           smallest fixing version per vulnerable package,
                            bump size (patch/minor/major), CVSS severity, rank
    package_versions        registry release history (PyPI / npm) for release
                            age and latest-version decisions
    run_tests               run the suite, parse the summary, log the run
                            with the dependency state it tested
    audit_diff              scope/test-integrity audit of a unified diff
    scan_patterns           old-API detector: regex match counts per rule
    export_patch            git diff of repo/ written to a deliverable

The baseline inventory, the client's test command and upgrade policy, and
the patch records are written by the specialist (agent.py), not by a tool
call the model can aim elsewhere.

Tools are plain functions `fn(workspace, *, fetch=None, run=None,
resolve_path=None, **args)`. Network and subprocess access only go through
the injected `fetch` / `run` (egress- and shell-policy checked by the kit),
and every model-supplied path through `resolve_path` (workspace jail,
inputs/ read-only, .agentkit/ refused). Snapshots live under
.agentkit/upgrade_migration/, which the model cannot write to directly, so
checks.py can trust them as recorded evidence.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from agentkit.policy import PolicyGate, executable_name, jail_path

STATE_DIR = ".agentkit/upgrade_migration"
OSV_API = "https://api.osv.dev/v1"

# Directories never scanned for manifests (vendored or generated trees).
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "vendor", "dist",
             "build", "__pycache__", ".tox", ".nox", ".mypy_cache", ".agentkit",
             "site-packages", "dist-packages", "__pypackages__", ".eggs"}

LOCKFILE_NAMES = {"package-lock.json", "poetry.lock", "Pipfile.lock", "go.sum", "uv.lock",
                  "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json"}
PY_LOCKS = ("poetry.lock", "uv.lock")
NPM_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")

# Dependency files of ecosystems v1 does not read. They are reported as
# "unknown" inventory rows so the plan has to name them (nothing is silently
# left unscanned). A manifest is not reported when its lockfile is beside it.
UNSUPPORTED_MANIFESTS = {
    "Cargo.lock": "Rust", "Cargo.toml": "Rust", "Gemfile.lock": "Ruby", "Gemfile": "Ruby",
    "composer.lock": "PHP", "composer.json": "PHP", "pom.xml": "Maven", "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle", "packages.lock.json": "NuGet", "bun.lockb": "Bun",
    "pubspec.lock": "Dart", "mix.lock": "Elixir", "Package.resolved": "Swift",
    "environment.yml": "conda", "setup.py": "setup.py", "setup.cfg": "setup.cfg",
}
_LOCK_FOR = {"Cargo.toml": "Cargo.lock", "Gemfile": "Gemfile.lock", "composer.json": "composer.lock"}
REMOVED = "removed"       # upgrade-log `to` for a package taken out of the repo
UNPINNED = "unpinned"     # upgrade-log `from` for a package that had no exact pin

# Generated trees left out of exported patches (a test run or a local venv
# must not end up in the customer's diff when the repo has no .gitignore).
PATCH_EXCLUDES = [f":(exclude,glob)**/{d}/**" for d in
                  ("__pycache__", ".pytest_cache", ".mypy_cache", ".tox", ".venv", "venv", "node_modules")]

# Markers that switch a test off (or narrow what runs). Adding one in a patch
# is a test-integrity violation unless the customer approved it.
SKIP_MARKERS = [
    r"@pytest\.mark\.skip", r"@pytest\.mark\.xfail", r"pytest\.skip\(", r"pytest\.xfail\(",
    r"\bpytestmark\s*=.*\b(skip|xfail)", r"\bimportorskip\(", r"add_marker\(.*\b(skip|xfail)",
    r"\bcollect_ignore(_glob)?\b", r"--deselect\b", r"--ignore(-glob)?(=|\s)", r"\baddopts\b.*\s-k\s",
    r"@unittest\.skip", r"@unittest\.expectedFailure", r"\bskipTest\(", r"\bSkipTest\b",
    r"\bit\.skip\(", r"\bdescribe\.skip\(", r"\btest\.skip\(", r"\bxit\(", r"\bxdescribe\(",
    r"\bxtest\(", r"\b(it|test|describe)\.only\(", r"\btestPathIgnorePatterns\b",
    r"@Disabled\b", r"@Ignore\b", r"\bt\.Skip(Now|f)?\(",
]
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_\w*|^\s*(?:it|test)\(\s*['\"`]|"
                      r"^\s*func\s+Test\w+\(|@Test\b")
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*\.py$|"
                       r"_test\.(py|go)$|\.(test|spec)\.[jt]sx?$")


# --- small helpers ----------------------------------------------------------

def _is_link(path: Path) -> bool:
    return path.is_symlink() or os.path.isjunction(path)


def walk_files(root: Path) -> list[Path]:
    """Regular files under `root`, sorted, never descending into SKIP_DIRS
    and never following a symlink or junction (it could point outside the
    workspace)."""
    out = []
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not _is_link(Path(here) / d))
        out += [Path(here) / n for n in names if not _is_link(Path(here) / n)]
    return sorted(out)


def registry_snapshot_path(workspace: Path, ecosystem: str, name: str) -> Path:
    return Path(workspace) / STATE_DIR / "registry" / f"{ecosystem}__{_safe_id(normalize_name(ecosystem, name))}.json"


def _state(workspace: Path) -> Path:
    path = Path(workspace) / STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _inside(workspace: Path, rel: str) -> Path:
    """Resolve a path that must stay inside `workspace`, checked lexically
    before it touches the filesystem (agentkit.policy.jail_path); raises
    PolicyViolation. For paths from the manifest or a patch, not the model."""
    return jail_path(Path(workspace), rel)


def _resolver(workspace: Path, resolve_path: Callable[..., Path] | None) -> Callable[..., Path]:
    """The kit's resolve_path when the tool runs in the harness (it also marks
    written files as agent-authored); the same PolicyGate rules when a
    function is called directly."""
    if resolve_path is not None:
        return resolve_path
    gate = PolicyGate()
    return lambda p, *, write=False: gate.resolve_path(Path(workspace), p, write=write)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_name(ecosystem: str, name: str) -> str:
    """PyPI names compare case- and separator-insensitively (PEP 503)."""
    if ecosystem == "PyPI":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


def dep_key(ecosystem: str, name: str, version: str | None) -> str:
    return f"{ecosystem}|{normalize_name(ecosystem, name)}|{version or ''}"


# --- versions ---------------------------------------------------------------

_VERSION = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(.*)$")


def version_key(version: str) -> tuple:
    """Sort key good enough for PEP 440 and SemVer release strings.

    Release numbers compare numerically; a pre-release suffix (a1, b2, rc1,
    -alpha, -beta.3) sorts before the final release, a post-release after it,
    and build metadata (+local) is ignored. "0" is the lowest version, which
    matches OSV's `introduced: "0"`.
    """
    version = version.strip().split("+", 1)[0]
    m = _VERSION.match(version)
    if not m:
        return ((0,), -1, ((1, version),))   # unparseable: lowest, still comparable
    release = tuple(int(p) for p in m.group(1).split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    suffix = m.group(2).lstrip(".-_").lower()
    if not suffix:
        rank = 1
    elif suffix.startswith(("post", "p", "r")) and not suffix.startswith(("pre", "rc")):
        rank = 2
    else:
        rank = 0
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-_]", suffix) if p)
    return (release, rank, parts)


def compare_versions(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def bump_kind(current: str, target: str) -> str:
    """patch | minor | major | none | downgrade, from the first differing part.

    For 0.x SemVer a minor bump is breaking, so it is reported as major.
    """
    if compare_versions(target, current) < 0:
        return "downgrade"
    if compare_versions(target, current) == 0:
        return "none"
    a = list(version_key(current)[0]) + [0, 0, 0]
    b = list(version_key(target)[0]) + [0, 0, 0]
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "major" if a[0] == 0 else "minor"
    return "patch"


# --- manifest / lockfile parsers --------------------------------------------

def _dep(ecosystem, name, version, source, *, direct=True, dev=False, pinned=True):
    return {"ecosystem": ecosystem, "name": normalize_name(ecosystem, name),
            "version": version, "source": source, "direct": direct, "dev": dev,
            "pinned": pinned and bool(version)}


_DEV_WORD = re.compile(r"(^|[-_.])(dev|develop|test|tests|testing|lint|ci|docs?)([-_.]|$)", re.I)
_REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$")
_EXACT = re.compile(r"===?([A-Za-z0-9.+!_-]+)")
_COMMENT = re.compile(r"(^|\s)#.*$")


def _requirement(line: Any, source: str, *, dev: bool) -> dict | None:
    """One PEP 508 requirement ("name[extra]==1.0 ; marker") as a row: an
    exact == pin is its version, anything else is kept as a constraint."""
    if not isinstance(line, str):
        return None
    m = _REQ_LINE.match(line.split(";", 1)[0].strip())
    if not m:
        return None
    name, spec = m.group(1), m.group(2).replace(" ", "")
    if spec and spec[0] not in "=<>!~@(":
        return None                       # prose, not a requirement
    pin = _EXACT.fullmatch(spec)
    return _dep("PyPI", name, pin.group(1) if pin else None, source,
                dev=dev, pinned=bool(pin)) | ({} if pin else {"constraint": spec})


def _spec_row(name: str, spec: Any, source: str, *, dev: bool, bare_exact: bool) -> dict:
    """A Poetry / Pipfile table entry (`name = "==1.0"` or `{version = ...}`).
    Poetry reads a bare "1.2.3" as an exact pin (bare_exact)."""
    if isinstance(spec, dict):
        spec = spec.get("version", json.dumps(spec, sort_keys=True))   # git/path/url sources
    spec = str(spec).replace(" ", "") if not isinstance(spec, list) else json.dumps(spec)
    pin = _EXACT.fullmatch(spec) or (re.fullmatch(r"(\d[A-Za-z0-9.+!_-]*)", spec) if bare_exact else None)
    return _dep("PyPI", name, pin.group(1) if pin else None, source,
                dev=dev, pinned=bool(pin)) | ({} if pin else {"constraint": spec})


def _logical_lines(text: str) -> list[str]:
    """pip requirement-file lines: comments dropped, backslash continuations
    joined, per-requirement options (--hash=..., --config-settings) cut."""
    out, buf = [], ""
    for raw in text.splitlines():
        line = _COMMENT.sub("", raw).rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(re.split(r"\s+--?[A-Za-z]", (buf + line).strip(), maxsplit=1)[0].strip())
        buf = ""
    if buf.strip():
        out.append(re.split(r"\s+--?[A-Za-z]", buf.strip(), maxsplit=1)[0].strip())
    return out


def is_requirements_file(rel: str) -> bool:
    """requirements*.txt, *-requirements.txt, constraints*.txt, and any .txt
    in a requirements/ folder (pip-compile outputs included)."""
    p = PurePosixPath(rel)
    name = p.name.lower()
    if not name.endswith(".txt"):
        return False
    return ("requirements" in name or name.startswith("constraints")
            or any(part.lower() in ("requirements", "reqs") for part in p.parts[:-1]))


def _parse_requirements(text: str, source: str) -> list[dict]:
    dev = bool(_DEV_WORD.search(PurePosixPath(source).name))
    out = []
    for line in _logical_lines(text):
        if not line or line.startswith(("-", "git+", "http:", "https:")):
            continue
        row = _requirement(line, source, dev=dev)
        if row:
            out.append(row)
    return out


def _parse_pyproject(text: str, source: str) -> list[dict]:
    """Declared requirements: [project] dependencies and optional-dependencies,
    [dependency-groups], [tool.uv] dev-dependencies and Poetry's tables."""
    data = tomllib.loads(text)
    tool = data.get("tool") or {}
    project = data.get("project") or {}
    rows = [_requirement(r, source, dev=False) for r in project.get("dependencies") or []]
    for extra, reqs in (project.get("optional-dependencies") or {}).items():
        rows += [_requirement(r, source, dev=bool(_DEV_WORD.search(extra))) for r in reqs or []]
    for reqs in (data.get("dependency-groups") or {}).values():
        rows += [_requirement(r, source, dev=True) for r in reqs or []]
    rows += [_requirement(r, source, dev=True) for r in (tool.get("uv") or {}).get("dev-dependencies") or []]
    poetry = tool.get("poetry") or {}
    tables = [(poetry.get("dependencies"), False), (poetry.get("dev-dependencies"), True)]
    tables += [((g or {}).get("dependencies"), name != "main") for name, g in (poetry.get("group") or {}).items()]
    for table, dev in tables:
        rows += [_spec_row(name, spec, source, dev=dev, bare_exact=True)
                 for name, spec in (table or {}).items() if name.lower() != "python"]
    return [r for r in rows if r]


def _parse_pipfile(text: str, source: str) -> list[dict]:
    data = tomllib.loads(text)
    return [_spec_row(name, spec, source, dev=dev, bare_exact=False)
            for section, dev in (("packages", False), ("dev-packages", True))
            for name, spec in (data.get(section) or {}).items()]


def _declared_names(rows: list[dict]) -> tuple[set[str], set[str]]:
    """(every declared name, names declared only as dev dependencies)."""
    names = {r["name"] for r in rows if r.get("name")}
    return names, names - {r["name"] for r in rows if not r.get("dev")}


def _declared(lock: Path, manifest: str, parse: Callable[[str, str], list[dict]]) -> tuple[set, set] | None:
    """Direct names from the manifest beside a lockfile; None if there is none."""
    path = lock.with_name(manifest)
    if not path.is_file() or _is_link(path):
        return None
    try:
        return _declared_names(parse(path.read_text(encoding="utf-8"), manifest))
    except (ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return None


def _is_direct(name: str, declared: tuple[set, set] | None) -> bool:
    """Unknown (no manifest to tell) counts as direct, so a newly added
    package is never waved through as transitive."""
    return True if declared is None else name in declared[0]


def _parse_poetry_lock(text: str, source: str, declared=None) -> list[dict]:
    data = tomllib.loads(text)
    out = []
    for pkg in data.get("package", []):
        groups = pkg.get("groups") or [pkg.get("category", "main")]
        out.append(_dep("PyPI", pkg["name"], str(pkg["version"]), source,
                        direct=_is_direct(normalize_name("PyPI", pkg["name"]), declared),
                        dev="main" not in groups))
    return out


def _parse_uv_lock(text: str, source: str, declared=None) -> list[dict]:
    """uv.lock: [[package]] entries; the project's own (editable / virtual)
    entries name the direct dependencies and are not rows themselves."""
    pkgs = [p for p in tomllib.loads(text).get("package", []) if isinstance(p, dict) and p.get("name")]
    roots = [p for p in pkgs if isinstance(p.get("source"), dict) and {"editable", "virtual"} & set(p["source"])]
    names, dev = (set(declared[0]), set(declared[1])) if declared else (set(), set())

    def dep_names(entries) -> set[str]:
        return {normalize_name("PyPI", d["name"]) for d in entries or [] if isinstance(d, dict) and d.get("name")}
    for root in roots:
        prod = dep_names(root.get("dependencies"))
        for group in (root.get("optional-dependencies") or {}).values():
            prod |= dep_names(group)
        devs = set().union(*(dep_names(g) for g in (root.get("dev-dependencies") or {}).values()))
        names |= prod | devs
        dev = (dev | devs) - prod
    known = declared is not None or bool(roots)
    root_ids = {id(r) for r in roots}
    return [_dep("PyPI", p["name"], str(p["version"]), source,
                 direct=normalize_name("PyPI", p["name"]) in names if known else True,
                 dev=normalize_name("PyPI", p["name"]) in dev)
            for p in pkgs if id(p) not in root_ids and p.get("version")]


def _parse_pipfile_lock(text: str, source: str, declared=None) -> list[dict]:
    data = json.loads(text)
    out = []
    for section, dev in (("default", False), ("develop", True)):
        for name, info in (data.get(section) or {}).items():
            version = str(info.get("version", "")).lstrip("=") or None
            out.append(_dep("PyPI", name, version, source,
                            direct=_is_direct(normalize_name("PyPI", name), declared), dev=dev))
    return out


def _package_json_rows(data: dict, source: str) -> list[dict]:
    out = []
    for section, dev in (("dependencies", False), ("optionalDependencies", False), ("devDependencies", True)):
        for name, spec in (data.get(section) or {}).items():
            exact = re.fullmatch(r"=?v?(\d+\.\d+\.\d+[^\s]*)", str(spec))
            out.append(_dep("npm", name, exact.group(1) if exact else None, source,
                            dev=dev, pinned=bool(exact)) | ({} if exact else {"constraint": spec}))
    return out


def _parse_package_json(text: str, source: str) -> list[dict]:
    return _package_json_rows(json.loads(text), source)


def _parse_package_lock(text: str, source: str, declared=None) -> list[dict]:
    data = json.loads(text)
    out = []
    packages = data.get("packages")
    if declared is None and isinstance(packages, dict) and isinstance(packages.get(""), dict):
        declared = _declared_names(_package_json_rows(packages[""], source))   # the lock's own root
    if isinstance(packages, dict):          # lockfileVersion 2 / 3
        for key, info in packages.items():
            if not key or "node_modules/" not in key or info.get("link"):
                continue
            name = key.rsplit("node_modules/", 1)[1]
            direct = key == f"node_modules/{name}" and _is_direct(name, declared)
            out.append(_dep("npm", name, info.get("version"), source,
                            direct=direct, dev=bool(info.get("dev"))))
    else:                                   # lockfileVersion 1: top level only
        for name, info in (data.get("dependencies") or {}).items():
            out.append(_dep("npm", name, info.get("version"), source,
                            direct=_is_direct(name, declared), dev=bool(info.get("dev"))))
    return out


_YARN_SKIP = re.compile(r"@(workspace|link|portal|patch|file|exec):")


def _parse_yarn_lock(text: str, source: str, declared=None) -> list[dict]:
    """yarn.lock, classic (`version "1.2.3"`) and berry (`version: 1.2.3`):
    one row per entry, named after its first descriptor."""
    out, header = [], None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            header = raw.strip().rstrip(":")
            continue
        m = re.fullmatch(r'  version:?\s+"?([^"\s]+)"?\s*', raw)
        if header is None or not m or _YARN_SKIP.search(header):
            continue
        first = header.split(",")[0].strip().strip('"')
        at = first.rfind("@")
        version = m.group(1)
        if at <= 0 or not version[:1].isdigit() or version.endswith("-use.local"):
            continue
        name = first[:at]
        out.append(_dep("npm", name, version, source, direct=_is_direct(name, declared),
                        dev=bool(declared) and name in declared[1]))
    return out


def _pnpm_ref(key: Any) -> tuple[str, str] | None:
    """(name, version) from a pnpm-lock packages key: "/name/1.0.0_peer" (v5),
    "/name@1.0.0(peer)" (v6) or "name@1.0.0" (v9)."""
    key = re.sub(r"\(.*$", "", str(key).lstrip("/"))
    m = re.fullmatch(r"(@[^/@]+/[^/@]+|[^/@]+)@([^/@]+)", key)
    version = m.group(2) if m else None
    if not m:
        m = re.fullmatch(r"(@[^/@]+/[^/@]+|[^/@]+)/([^/]+)", key)
        version = m.group(2).split("_", 1)[0] if m else None
    return (m.group(1), version) if m and version[:1].isdigit() else None


def _parse_pnpm_lock(text: str, source: str) -> list[dict]:
    import yaml

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(str(exc)) from None
    if not isinstance(data, dict):
        raise ValueError("not a mapping")
    direct, dev, prod = set(), set(), set()
    for imp in (data.get("importers") or {".": data}).values():
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for name, ref in ((imp or {}).get(section) or {}).items():
                version = ref.get("version") if isinstance(ref, dict) else ref
                pair = (name, re.sub(r"\(.*$", "", str(version)).split("_", 1)[0])
                direct.add(pair)
                (dev if section == "devDependencies" else prod).add(pair)
    out = []
    for key, info in (data.get("packages") or {}).items():
        ref = _pnpm_ref(key)
        if ref is None:
            continue
        info = info if isinstance(info, dict) else {}
        is_dev = bool(info["dev"]) if "dev" in info else (ref in dev and ref not in prod)
        out.append(_dep("npm", ref[0], ref[1], source, direct=ref in direct, dev=is_dev))
    return out


def _parse_go_mod(text: str, source: str) -> list[dict]:
    out = []
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if in_block or line.startswith("require "):
            body = line[len("require "):] if line.startswith("require ") else line
            m = re.match(r"^(\S+)\s+(v\S+)(\s*//\s*indirect)?", body)
            if m:
                out.append(_dep("Go", m.group(1), m.group(2), source, direct=not m.group(3)))
    return out


def _unsupported(path: Path, source: str) -> dict | None:
    kind = UNSUPPORTED_MANIFESTS.get(path.name)
    lock = _LOCK_FOR.get(path.name)
    if kind is None or (lock and path.with_name(lock).exists()):
        return None
    if path.name == "setup.cfg" and "install_requires" not in path.read_text(encoding="utf-8", errors="replace"):
        return None
    what = f"dependencies declared in {kind} are" if kind.startswith("setup") else f"{kind} dependencies are"
    return {"ecosystem": "unknown", "name": source, "version": None, "source": source,
            "error": f"not read: {what} outside what v1 inventories (PyPI, npm, Go lockfiles/manifests)"}


def _parse_file(path: Path, source: str) -> list[dict]:
    """Rows from one file, chosen by name before anything is read. A
    manifest beside its lockfile only marks the lockfile's direct entries."""
    name = path.name

    def text() -> str:
        return path.read_text(encoding="utf-8")

    def beside(*names: str) -> bool:
        return any(path.with_name(n).is_file() for n in names)
    if is_requirements_file(source):
        return _parse_requirements(text(), source)
    if name == "pyproject.toml":
        return [] if beside(*PY_LOCKS) else _parse_pyproject(text(), source)
    if name == "Pipfile":
        return [] if beside("Pipfile.lock") else _parse_pipfile(text(), source)
    if name == "poetry.lock":
        return _parse_poetry_lock(text(), source, _declared(path, "pyproject.toml", _parse_pyproject))
    if name == "uv.lock":
        return _parse_uv_lock(text(), source, _declared(path, "pyproject.toml", _parse_pyproject))
    if name == "Pipfile.lock":
        return _parse_pipfile_lock(text(), source, _declared(path, "Pipfile", _parse_pipfile))
    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        return _parse_package_lock(text(), source, _declared(path, "package.json", _parse_package_json))
    if name == "yarn.lock":
        return _parse_yarn_lock(text(), source, _declared(path, "package.json", _parse_package_json))
    if name == "pnpm-lock.yaml":
        return _parse_pnpm_lock(text(), source)
    if name == "package.json":
        return [] if beside(*NPM_LOCKS) else _parse_package_json(text(), source)
    if name == "go.mod":
        return _parse_go_mod(text(), source)
    row = _unsupported(path, source)
    return [row] if row else []


def collect_dependencies(repo: Path) -> list[dict]:
    """Every dependency found under `repo`, lockfiles preferred over manifests.

    A manifest (package.json, pyproject.toml, Pipfile) is only used for
    versions when no lockfile sits beside it; its names still mark lockfile
    entries as direct. Files of ecosystems v1 does not read, and files that
    do not parse, become "unknown" rows carrying an error.
    """
    repo = Path(repo)
    found: list[dict] = []
    for path in walk_files(repo):
        source = path.relative_to(repo).as_posix()
        try:
            found += _parse_file(path, source)
        except (ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as exc:
            found.append({"ecosystem": "unknown", "name": source, "version": None,
                          "source": source, "error": f"unparseable: {exc}"})
    # Same package+version from several files: keep one row, merge flags.
    merged: dict[str, dict] = {}
    for dep in found:
        key = dep_key(dep["ecosystem"], dep["name"], dep["version"])
        if dep["version"] is None:           # unpinned: one row per declaring file
            key += "|" + dep["source"]
        if key in merged:
            merged[key]["direct"] = merged[key]["direct"] or dep.get("direct", False)
            merged[key]["dev"] = merged[key]["dev"] and dep.get("dev", False)
            merged[key]["sources"] = sorted(set(merged[key]["sources"]) | {dep["source"]})
        else:
            merged[key] = dict(dep, sources=[dep["source"]])
            merged[key].pop("source", None)
    return sorted(merged.values(), key=lambda d: (d["ecosystem"], d["name"], version_key(d["version"] or ""),
                                                  d["version"] or "", d["sources"]))


def unread_manifests(deps: list[dict]) -> list[str]:
    """Dependency files the inventory could not read (unsupported or broken)."""
    return sorted(src for d in deps if d["ecosystem"] == "unknown" for src in d["sources"])


def dep_state(deps: list[dict]) -> dict[str, list[str]]:
    """{"ecosystem|name": sorted exact versions} for every declared package.
    An empty list means declared but pinned nowhere; a missing key means
    not declared at all. Every installed copy of a package counts (an npm
    lockfile can hold several versions of one name)."""
    state: dict[str, set[str]] = {}
    for d in deps:
        if d["ecosystem"] == "unknown":
            continue
        versions = state.setdefault(f"{d['ecosystem']}|{d['name']}", set())
        if d.get("version"):
            versions.add(d["version"])
    return {k: sorted(v, key=lambda x: (version_key(x), x)) for k, v in sorted(state.items())}


def state_sha256(state: dict[str, list[str]]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def baseline_path(workspace: Path) -> Path:
    return Path(workspace) / STATE_DIR / "baseline.json"


def record_baseline(workspace: Path, repo: str = "repo") -> bool:
    """Write the baseline inventory of the whole repo/ once (the specialist
    does this before the first milestone's loop). Later calls never replace
    it. Returns True when this call wrote it."""
    path = baseline_path(workspace)
    root = Path(workspace) / repo
    if path.exists() or not root.is_dir():
        return False
    _write_json(path, {"recorded_at": _now(), "path": repo, "dependencies": collect_dependencies(root)})
    return True


def _is_repo_root(workspace: Path, resolved: Path) -> bool:
    return Path(resolved).resolve() == (Path(workspace) / "repo").resolve()


def inventory_dependencies(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                           path: str = "repo", output: str | None = None, **_: Any) -> dict:
    """Parse the repo's dependencies. If no baseline was recorded yet (the
    specialist records it before the loop), a call over the whole repo/
    writes it; a call over a subfolder never does."""
    resolve = _resolver(workspace, resolve_path)
    repo = resolve(path)
    if not repo.is_dir():
        return {"error": f"{path}/ does not exist"}
    deps = collect_dependencies(repo)
    if _is_repo_root(workspace, repo):
        record_baseline(workspace)
    result = {
        "count": len(deps),
        "pinned": sum(1 for d in deps if d.get("pinned")),
        "unpinned": [f"{d['ecosystem']}:{d['name']} {d.get('constraint', '')}".strip()
                     for d in deps if not d.get("pinned") and d["ecosystem"] != "unknown"],
        "errors": [f"{d['name']}: {d['error']}" for d in deps if d.get("error")],
        "baseline_recorded": baseline_path(workspace).exists(),
        "dependencies": deps,
    }
    if output:
        _write_json(resolve(output, write=True), {"path": path, "dependencies": deps})
        result["written"] = output
    return result


# --- OSV ----------------------------------------------------------------------

def _response_json(resp) -> Any:
    text = getattr(resp, "text", None)
    if text is None:
        body = getattr(resp, "body", b"")
        text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    return json.loads(text or "null")


def _safe_id(vuln_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", vuln_id)


def load_osv_state(workspace: Path) -> tuple[dict, dict]:
    """(queries, vulns): recorded query answers keyed by dep_key and the
    advisory records keyed by id."""
    state = Path(workspace) / STATE_DIR / "osv"
    queries = {}
    qfile = state / "queries.json"
    if qfile.exists():
        queries = json.loads(qfile.read_text(encoding="utf-8")).get("queries", {})
    vulns = {}
    vdir = state / "vulns"
    if vdir.is_dir():
        for f in sorted(vdir.glob("*.json")):
            rec = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and rec.get("id"):
                vulns[rec["id"]] = rec
    return queries, vulns


def osv_scan(workspace: Path, *, fetch: Callable | None = None, run=None, resolve_path=None,
             path: str = "repo", **_: Any) -> dict:
    """Query OSV for every pinned dependency currently in the repo, and for
    the baseline's pinned versions (so the before/after comparison never
    lacks evidence), and store the answers plus each advisory's full record
    as evidence."""
    if fetch is None:
        return {"error": "network access is not available to this tool"}
    all_deps = collect_dependencies(_resolver(workspace, resolve_path)(path))
    scannable = ("PyPI", "npm", "Go")
    deps = [d for d in all_deps if d.get("version") and d["ecosystem"] in scannable]
    seen = {dep_key(d["ecosystem"], d["name"], d["version"]) for d in deps}
    base = json.loads(baseline_path(workspace).read_text(encoding="utf-8")).get("dependencies", []) \
        if baseline_path(workspace).is_file() else []
    extra = {dep_key(d["ecosystem"], d["name"], d["version"]): d for d in base
             if d.get("version") and d["ecosystem"] in scannable}
    extra = [d for k, d in sorted(extra.items()) if k not in seen]
    state = _state(workspace) / "osv"
    queries, known = load_osv_state(workspace)
    errors: list[str] = []
    to_query = deps + extra
    for start in range(0, len(to_query), 500):
        chunk = to_query[start:start + 500]
        body = {"queries": [{"package": {"ecosystem": d["ecosystem"], "name": d["name"]},
                             "version": d["version"]} for d in chunk]}
        resp = fetch(f"{OSV_API}/querybatch", method="POST",
                     headers={"Content-Type": "application/json"},
                     body=json.dumps(body).encode("utf-8"))
        if getattr(resp, "status", 0) != 200:
            errors.append(f"querybatch HTTP {getattr(resp, 'status', '?')}")
            continue
        results = (_response_json(resp) or {}).get("results", [])
        for dep, res in zip(chunk, results):
            ids = sorted({v["id"] for v in (res or {}).get("vulns", []) if v.get("id")})
            entry = {"ids": ids, "retrieved_at": _now()}
            if (res or {}).get("next_page_token"):
                entry["truncated"] = True
            queries[dep_key(dep["ecosystem"], dep["name"], dep["version"])] = entry
    wanted = sorted({i for d in to_query for i in queries.get(
        dep_key(d["ecosystem"], d["name"], d["version"]), {}).get("ids", [])} - set(known))
    for vuln_id in wanted:
        resp = fetch(f"{OSV_API}/vulns/{vuln_id}")
        if getattr(resp, "status", 0) != 200:
            errors.append(f"{vuln_id}: HTTP {getattr(resp, 'status', '?')}")
            continue
        record = _response_json(resp)
        if not isinstance(record, dict) or record.get("id") != vuln_id:
            errors.append(f"{vuln_id}: record id mismatch")
            continue
        record["_retrieved_at"] = _now()
        _write_json(state / "vulns" / f"{_safe_id(vuln_id)}.json", record)
    _write_json(state / "queries.json", {"api": OSV_API, "queries": queries})
    findings = findings_for(deps, *load_osv_state(workspace))
    return {"scanned": len(deps), "baseline_versions_scanned": len(extra),
            "vulnerable_packages": len({(f["ecosystem"], f["name"], f["version"]) for f in findings}),
            "findings": findings, "errors": errors,
            "skipped_unpinned": sorted({d["name"] for d in all_deps
                                        if not d.get("version") and d["ecosystem"] != "unknown"}),
            "not_read": unread_manifests(all_deps)}


def _events_intervals(events: list[dict]) -> list[tuple[str, str | None, bool]]:
    """(introduced, end, end_inclusive) intervals from an OSV range's events."""
    intervals = []
    start = None
    for ev in sorted(events, key=lambda e: version_key(next(iter(e.values())))):
        if "introduced" in ev:
            start = ev["introduced"]
        elif start is not None and ("fixed" in ev or "last_affected" in ev):
            end = ev.get("fixed") or ev.get("last_affected")
            intervals.append((start, end, "last_affected" in ev))
            start = None
    if start is not None:
        intervals.append((start, None, False))
    return intervals


def _affected_entries(record: dict, ecosystem: str, name: str) -> list[dict]:
    return [a for a in record.get("affected", [])
            if a.get("package", {}).get("ecosystem") == ecosystem
            and normalize_name(ecosystem, a.get("package", {}).get("name", "")) == normalize_name(ecosystem, name)]


def is_affected(record: dict, ecosystem: str, name: str, version: str) -> bool:
    """Evaluate an OSV record's ranges/versions locally for one version."""
    for aff in _affected_entries(record, ecosystem, name):
        if version in (aff.get("versions") or []):
            return True
        for rng in aff.get("ranges", []):
            if rng.get("type") not in ("SEMVER", "ECOSYSTEM"):
                continue
            for lo, hi, inclusive in _events_intervals(rng.get("events", [])):
                if compare_versions(version, lo) < 0 and lo != "0":
                    continue
                if hi is None or compare_versions(version, hi) < 0 or (inclusive and compare_versions(version, hi) == 0):
                    return True
    return False


def fixed_version(record: dict, ecosystem: str, name: str, version: str) -> str | None:
    """Smallest `fixed` version that ends the interval containing `version`."""
    best = None
    for aff in _affected_entries(record, ecosystem, name):
        for rng in aff.get("ranges", []):
            if rng.get("type") not in ("SEMVER", "ECOSYSTEM"):
                continue
            for lo, hi, inclusive in _events_intervals(rng.get("events", [])):
                inside = (lo == "0" or compare_versions(version, lo) >= 0) and hi is not None and \
                    compare_versions(version, hi) < 0 and not inclusive
                if inside and (best is None or compare_versions(hi, best) < 0):
                    best = hi
    return best


# CVSS v3.x base-score weights, from the FIRST specification.
_CVSS_W = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _roundup(value: float) -> float:
    as_int = round(value * 100000)
    if as_int % 10000 == 0:
        return as_int / 100000.0
    return (math.floor(as_int / 10000) + 1) / 10.0


def cvss3_base_score(vector: str) -> float | None:
    """Base score of a CVSS:3.0/3.1 vector string; None if it does not parse."""
    if not vector.startswith(("CVSS:3.0/", "CVSS:3.1/")):
        return None
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        changed = m["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[m["PR"]]
        iss = 1 - (1 - _CVSS_W["CIA"][m["C"]]) * (1 - _CVSS_W["CIA"][m["I"]]) * (1 - _CVSS_W["CIA"][m["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
        exploit = 8.22 * _CVSS_W["AV"][m["AV"]] * _CVSS_W["AC"][m["AC"]] * pr * _CVSS_W["UI"][m["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    total = 1.08 * (impact + exploit) if changed else impact + exploit
    return _roundup(min(total, 10.0))


def severity_label(score: float | None) -> str:
    if score is None:
        return "UNKNOWN"
    if score == 0:
        return "NONE"
    return "LOW" if score < 4 else "MEDIUM" if score < 7 else "HIGH" if score < 9 else "CRITICAL"


def record_severity(record: dict) -> tuple[float | None, str]:
    """Highest CVSS v3 score in the record, else the database's own label."""
    scores = [cvss3_base_score(s.get("score", "")) for s in record.get("severity", [])
              if s.get("type", "").startswith("CVSS_V3")]
    scores = [s for s in scores if s is not None]
    if scores:
        return max(scores), severity_label(max(scores))
    label = str((record.get("database_specific") or {}).get("severity", "")).upper()
    label = {"MODERATE": "MEDIUM"}.get(label, label)
    return None, label if label in ("LOW", "MEDIUM", "HIGH", "CRITICAL") else "UNKNOWN"


def findings_for(deps: list[dict], queries: dict, vulns: dict) -> list[dict]:
    """One row per (dependency, advisory) from recorded OSV answers."""
    rows = []
    for d in deps:
        if not d.get("version"):
            continue
        entry = queries.get(dep_key(d["ecosystem"], d["name"], d["version"]))
        for vuln_id in (entry or {}).get("ids", []):
            rec = vulns.get(vuln_id, {})
            score, label = record_severity(rec) if rec else (None, "UNKNOWN")
            rows.append({
                "ecosystem": d["ecosystem"], "name": d["name"], "version": d["version"],
                "vuln_id": vuln_id, "aliases": sorted(rec.get("aliases", [])),
                "summary": rec.get("summary", ""), "cvss": score, "severity": label,
                "fixed_in": fixed_version(rec, d["ecosystem"], d["name"], d["version"]) if rec else None,
                "direct": d.get("direct", False), "dev": d.get("dev", False),
            })
    return rows


FINDINGS_COLUMNS = ["ecosystem", "name", "version", "vuln_id", "aliases", "severity",
                    "cvss", "fixed_in", "direct", "dev", "summary"]
_SEV_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0, "UNKNOWN": 0}


def plan_upgrades(workspace: Path, *, fetch=None, run=None, resolve_path=None, path: str = "repo",
                  findings_csv: str | None = None, output: str | None = None, **_: Any) -> dict:
    """Candidate upgrade list from the recorded OSV evidence.

    For each vulnerable package: the smallest version that fixes every
    advisory with a known fix, its bump size, and whether any advisory stays
    open (no fix, or the target is still affected). Ranked by severity, then
    advisories fixed per change, then smallest bump.
    """
    import csv
    resolve = _resolver(workspace, resolve_path)
    deps = collect_dependencies(resolve(path))
    queries, vulns = load_osv_state(workspace)
    unscanned = [d["name"] for d in deps if d.get("version") and d["ecosystem"] in ("PyPI", "npm", "Go")
                 and dep_key(d["ecosystem"], d["name"], d["version"]) not in queries]
    findings = findings_for(deps, queries, vulns)
    by_pkg: dict[tuple, list[dict]] = {}
    for f in findings:
        by_pkg.setdefault((f["ecosystem"], f["name"], f["version"]), []).append(f)
    plan = []
    for (eco, name, version), rows in by_pkg.items():
        fixes = [r["fixed_in"] for r in rows if r["fixed_in"]]
        target = max(fixes, key=version_key) if fixes else None
        still_open = [r["vuln_id"] for r in rows if not r["fixed_in"]]
        if target:
            still_open += [r["vuln_id"] for r in rows if r["fixed_in"] and r["vuln_id"] in vulns
                           and is_affected(vulns[r["vuln_id"]], eco, name, target)]
        worst = max(rows, key=lambda r: (_SEV_RANK.get(r["severity"], 0), r["cvss"] or 0))
        plan.append({
            "ecosystem": eco, "name": name, "current": version, "target": target,
            "bump": bump_kind(version, target) if target else "none",
            "fixes": sorted(r["vuln_id"] for r in rows if r["vuln_id"] not in still_open),
            "open": sorted(set(still_open)), "max_severity": worst["severity"],
            "direct": any(r["direct"] for r in rows), "dev": all(r["dev"] for r in rows),
        })
    bump_rank = {"patch": 0, "minor": 1, "major": 2, "none": 3, "downgrade": 4}
    plan.sort(key=lambda p: (-_SEV_RANK.get(p["max_severity"], 0), -len(p["fixes"]),
                             bump_rank[p["bump"]], p["name"]))
    for i, item in enumerate(plan, 1):
        item["rank"] = i
        item["risk_tier"] = {"patch": "low", "minor": "medium"}.get(item["bump"], "high")
    result = {"findings": len(findings), "packages": len(plan), "unscanned": unscanned, "plan": plan}
    if findings_csv:
        target_path = resolve(findings_csv, write=True)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FINDINGS_COLUMNS)
            writer.writeheader()
            for f in findings:
                writer.writerow({k: (";".join(f[k]) if isinstance(f[k], list) else f[k]) for k in FINDINGS_COLUMNS})
        result["findings_csv"] = findings_csv
    if output:
        _write_json(resolve(output, write=True), {"plan": plan, "unscanned": unscanned})
        result["written"] = output
    return result


# --- registry release history -----------------------------------------------

DEFAULT_MIN_AGE_DAYS = 7


def record_policy(workspace: Path, intake: dict | None) -> dict:
    """Store the parts of the client's upgrade policy the checks enforce as
    tool-owned evidence: the minimum release age ("... 30 days ..." in
    upgrade_policy; the default is 7 days, and a policy can only raise it)."""
    text = str((intake or {}).get("upgrade_policy") or "")
    days = [int(n) for n in re.findall(r"(\d+)\s*-?\s*days?\b", text, re.I)]
    policy = {"min_release_age_days": max([DEFAULT_MIN_AGE_DAYS, *days]),
              "source": "intake.upgrade_policy" if days else "default"}
    _write_json(_state(workspace) / "policy.json", dict(policy, at=_now()))
    return policy


def min_release_age(workspace: Path) -> int:
    path = Path(workspace) / STATE_DIR / "policy.json"
    try:
        value = int(json.loads(path.read_text(encoding="utf-8")).get("min_release_age_days"))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_MIN_AGE_DAYS
    return max(value, DEFAULT_MIN_AGE_DAYS)


def package_versions(workspace: Path, *, fetch: Callable | None = None, run=None, ecosystem: str = "",
                     name: str = "", min_age_days: int | None = None, **_: Any) -> dict:
    """Release history from PyPI or npm, snapshotted, with each version's age.

    Versions younger than `min_age_days` (default: the client's policy, at
    least 7 days) are flagged: a fresh release is the usual vector for a
    compromised package, so the plan should not pick one.
    """
    if fetch is None:
        return {"error": "network access is not available to this tool"}
    min_age_days = min_release_age(workspace) if min_age_days is None else int(min_age_days)
    if ecosystem == "PyPI":
        url = f"https://pypi.org/pypi/{normalize_name('PyPI', name)}/json"
    elif ecosystem == "npm":
        url = f"https://registry.npmjs.org/{name.replace('/', '%2F')}"
    else:
        return {"error": f"unsupported ecosystem {ecosystem!r} (PyPI or npm)"}
    resp = fetch(url)
    if getattr(resp, "status", 0) != 200:
        return {"error": f"HTTP {getattr(resp, 'status', '?')} from {url}"}
    data = _response_json(resp) or {}
    released: dict[str, str] = {}
    yanked: list[str] = []
    if ecosystem == "PyPI":
        for version, files in (data.get("releases") or {}).items():
            times = sorted(f.get("upload_time_iso_8601") or f.get("upload_time", "") for f in files)
            if times:
                released[version] = times[0]
            if files and all(f.get("yanked") for f in files):
                yanked.append(version)
        latest = (data.get("info") or {}).get("version")
    else:
        released = {v: t for v, t in (data.get("time") or {}).items() if v not in ("created", "modified")}
        latest = (data.get("dist-tags") or {}).get("latest")
        yanked = [v for v, info in (data.get("versions") or {}).items() if info.get("deprecated")]
    now = datetime.now(timezone.utc)
    versions = []
    for version, stamp in sorted(released.items(), key=lambda kv: version_key(kv[0])):
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age = (now - when).days
        except ValueError:
            age = None
        versions.append({"version": version, "released": stamp, "age_days": age,
                         "too_new": age is not None and age < min_age_days,
                         "yanked": version in yanked})
    snapshot = {"url": url, "retrieved_at": _now(), "latest": latest, "versions": versions}
    _write_json(registry_snapshot_path(workspace, ecosystem, name), snapshot)
    return {"ecosystem": ecosystem, "name": name, "latest": latest,
            "count": len(versions), "versions": versions[-40:]}


# --- tests --------------------------------------------------------------------

_COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected|todo|total)\b")


def parse_test_summary(output: str) -> dict:
    """Counts from a pytest / jest / vitest / go test style summary.

    The last occurrence of each counter in the final lines wins, so warnings
    printed earlier do not leak in. go test has no totals: count ok/FAIL lines.
    """
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    tail = output.splitlines()[-40:]
    for line in tail:
        for n, word in _COUNT.findall(line):
            key = {"error": "errors", "xfailed": "skipped", "todo": "skipped",
                   "deselected": "skipped"}.get(word, word)
            if key in counts:
                counts[key] = int(n)
    if not any(counts.values()):
        counts["passed"] = sum(1 for l in output.splitlines() if re.match(r"^(ok|--- PASS)\s", l))
        counts["failed"] = sum(1 for l in output.splitlines() if re.match(r"^(FAIL|--- FAIL)\s", l))
    return counts


def run_tests(workspace: Path, *, fetch=None, run: Callable | None = None, resolve_path=None,
              argv: list[str] | None = None, cwd: str = "repo", label: str = "",
              timeout: int | None = None, **_: Any) -> dict:
    """Run the test command, parse the summary and append the run to the
    tool-owned test log with the repo's dependency state at that moment
    (checks use both to prove which versions each green run tested)."""
    if run is None:
        return {"error": "command execution is not available to this tool"}
    if not argv:
        return {"error": "argv is required, e.g. [\"python\", \"-m\", \"pytest\", \"-q\"]"}
    resolved = _resolver(workspace, resolve_path)(cwd)
    cwd_rel = resolved.relative_to(Path(workspace).resolve()).as_posix()
    repo = Path(workspace) / "repo"
    state = dep_state(collect_dependencies(repo)) if repo.is_dir() else {}
    res = run(list(argv), cwd=cwd, timeout=timeout)
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    counts = parse_test_summary(out)
    log = _state(workspace) / "test_runs.jsonl"
    n = sum(1 for _ in log.open(encoding="utf-8")) + 1 if log.exists() else 1
    run_id = f"run-{n}"
    _write_json(_state(workspace) / "test_runs" / f"{run_id}.json", {"id": run_id, "dependencies": state})
    entry = {"id": run_id, "label": label, "argv": list(argv), "cwd": cwd_rel,
             "client_command": list(argv) == recorded_test_command(workspace),
             "exit_code": res.exit_code, "timed_out": bool(getattr(res, "timed_out", False)),
             "counts": counts, "output_sha256": hashlib.sha256(out.encode("utf-8")).hexdigest(),
             "deps_sha256": state_sha256(state), "at": _now()}
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return dict(entry, green=is_green(entry), tail="\n".join(out.strip().splitlines()[-25:]))


def is_green(entry: dict) -> bool:
    counts = entry.get("counts") or {}
    return (entry.get("exit_code") == 0 and not entry.get("timed_out")
            and not counts.get("failed") and not counts.get("errors"))


def load_test_runs(workspace: Path) -> list[dict]:
    log = Path(workspace) / STATE_DIR / "test_runs.jsonl"
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_run_state(workspace: Path, entry: dict) -> dict[str, list[str]] | None:
    """The dependency state recorded with a test run, or None when it is
    missing or does not match the hash in the log."""
    path = Path(workspace) / STATE_DIR / "test_runs" / f"{_safe_id(str(entry.get('id', '')))}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")).get("dependencies")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(state, dict) or state_sha256(state) != entry.get("deps_sha256"):
        return None
    return state


def command_argv(command: Any) -> list[str] | None:
    """An intake test_command ("python -m pytest -q" or an argv list) as argv."""
    if isinstance(command, str):
        argv = shlex.split(command)
    elif isinstance(command, list) and all(isinstance(a, str) for a in command):
        argv = list(command)
    else:
        return None
    return argv or None


def record_test_command(workspace: Path, command: Any) -> list[str] | None:
    """Store the client's test command as tool-owned evidence, so tests_pass
    re-runs exactly what the client named (not what the model chose)."""
    argv = command_argv(command)
    if argv:
        _write_json(_state(workspace) / "test_command.json", {"argv": argv, "at": _now()})
    return argv


def recorded_test_command(workspace: Path) -> list[str] | None:
    path = Path(workspace) / STATE_DIR / "test_command.json"
    if not path.is_file():
        return None
    return command_argv(json.loads(path.read_text(encoding="utf-8")).get("argv"))


_SHELL_TOKENS = {"&&", "||", "|", ";", "&", ">", ">>", "<", "2>", "2>&1"}


def test_command_problem(command: Any, allowed: list[str]) -> str | None:
    """Why an intake test_command cannot be run as given, or None. It must be
    one argv (no shell operators, env assignments or `cd`) whose program is
    on the specialist's shell allowlist."""
    try:
        argv = command_argv(command)
    except ValueError as exc:
        return f"the test command does not parse ({exc})"
    if not argv:
        return "the test command is empty"
    if any(a in _SHELL_TOKENS or "$(" in a or "`" in a for a in argv):
        return "the test command uses shell syntax (&&, |, ;, redirects), which is not run through a shell"
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]) or argv[0] == "cd":
        return "the test command sets environment variables or changes directory, which a single argv cannot do"
    if executable_name(argv[0]) not in {executable_name(a) for a in allowed}:
        return (f"the test command runs {argv[0]!r}, which is not one of the programs this "
                f"specialist may run ({', '.join(sorted(allowed))})")
    return None


# --- diff audit -----------------------------------------------------------------

def parse_unified_diff(text: str) -> list[dict]:
    """Files in a unified (git) diff with their added and removed lines."""
    files: list[dict] = []
    cur = None
    old_left = new_left = 0          # lines still owed by the current hunk

    def start() -> dict:
        entry = {"old": None, "new": None, "added": [], "removed": [], "status": "modified"}
        files.append(entry)
        return entry

    for line in text.splitlines():
        if old_left > 0 or new_left > 0:   # inside a hunk: count lines, not prefixes
            if line.startswith("+"):
                cur["added"].append(line[1:])
                new_left -= 1
            elif line.startswith("-"):
                cur["removed"].append(line[1:])
                old_left -= 1
            elif line.startswith("\\"):
                pass                       # "\ No newline at end of file"
            else:
                old_left -= 1
                new_left -= 1
            continue
        hunk = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
        if hunk and cur is not None:
            old_left = int(hunk.group(1) or 1)
            new_left = int(hunk.group(2) or 1)
            continue
        if line.startswith("diff --git "):
            cur = start()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            continue
        if line.startswith("--- ") and (cur is None or cur["added"] or cur["removed"]):
            cur = start()                  # plain (non-git) unified diff
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("--- "):
            p = line[4:].strip()
            cur["old"] = None if p == "/dev/null" else re.sub(r"^a/", "", p)
            if p == "/dev/null":
                cur["status"] = "added"
        elif line.startswith("+++ "):
            p = line[4:].strip()
            cur["new"] = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            if p == "/dev/null":
                cur["status"] = "deleted"
    for f in files:
        f["path"] = f["new"] or f["old"] or ""
    return [f for f in files if f["path"]]


def _match_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch("/" + path, "*/" + g.lstrip("*/")) for g in globs)


def audit_patch_text(text: str, *, allow: list[str] | None = None, forbid: list[str] | None = None) -> dict:
    """Scope and test-integrity audit of a unified diff (shared with checks.py)."""
    files = parse_unified_diff(text)
    skip_re = re.compile("|".join(SKIP_MARKERS))
    report = {"files": [], "changed_lines": 0, "lockfile_lines": 0, "skips_added": [],
              "tests_removed": 0, "tests_added": 0, "test_files_deleted": [],
              "outside_allow": [], "forbidden": []}
    for f in files:
        path = f["path"]
        is_lock = Path(path).name in LOCKFILE_NAMES
        n = len(f["added"]) + len(f["removed"])
        report["files"].append({"path": path, "status": f["status"], "lines": n, "lockfile": is_lock})
        report["lockfile_lines" if is_lock else "changed_lines"] += n
        report["skips_added"] += [f"{path}: {l.strip()}" for l in f["added"] if skip_re.search(l)]
        removed_defs = sum(1 for l in f["removed"] if TEST_DEF.search(l))
        added_defs = sum(1 for l in f["added"] if TEST_DEF.search(l))
        report["tests_removed"] += removed_defs
        report["tests_added"] += added_defs
        if f["status"] == "deleted" and TEST_PATH.search(path):
            report["test_files_deleted"].append(path)
        if allow and not _match_any(path, allow):
            report["outside_allow"].append(path)
        if forbid and _match_any(path, forbid):
            report["forbidden"].append(path)
    report["net_tests"] = report["tests_added"] - report["tests_removed"]
    report["clean"] = not (report["skips_added"] or report["test_files_deleted"] or
                           report["outside_allow"] or report["forbidden"] or report["net_tests"] < 0)
    return report


def audit_diff(workspace: Path, *, fetch=None, run=None, resolve_path=None, patch: str = "",
               allow: list[str] | None = None, forbid: list[str] | None = None, **_: Any) -> dict:
    """Audit a patch file in the workspace before submitting it."""
    target = _resolver(workspace, resolve_path)(patch)
    if not target.is_file():
        return {"error": f"{patch} not found"}
    return audit_patch_text(target.read_text(encoding="utf-8", errors="replace"), allow=allow, forbid=forbid)


# --- old-API detector -------------------------------------------------------------

def count_pattern_matches(repo: Path, detectors: list[dict]) -> dict[str, dict]:
    """{detector id: {"count", "sites": [path:line, ...]}} over files matching
    each detector's glob (default: every text file)."""
    repo = Path(repo)
    files = walk_files(repo)
    out: dict[str, dict] = {}
    for det in detectors:
        rx = re.compile(det["regex"])
        glob = det.get("glob", "*")
        sites = []
        for p in files:
            rel = p.relative_to(repo).as_posix()
            if not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(p.name, glob)):
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            sites += [f"{rel}:{i}" for i, line in enumerate(lines, 1) if rx.search(line)]
        out[det["id"]] = {"count": len(sites), "sites": sites}
    return out


def scan_patterns(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                  detectors: list[dict] | None = None, path: str = "repo", **_: Any) -> dict:
    """Count old-API usages. The first scan of each detector id records its
    baseline (regex + count); later scans show progress toward zero."""
    detectors = detectors or []
    for det in detectors:
        if not det.get("id") or not det.get("regex"):
            return {"error": "each detector needs an id and a regex"}
        try:
            re.compile(det["regex"])
        except re.error as exc:
            return {"error": f"{det['id']}: bad regex: {exc}"}
    counts = count_pattern_matches(_resolver(workspace, resolve_path)(path), detectors)
    base_file = _state(workspace) / "detector_baseline.json"
    baseline = json.loads(base_file.read_text(encoding="utf-8")) if base_file.exists() else {}
    for det in detectors:
        if det["id"] not in baseline:
            baseline[det["id"]] = {"regex": det["regex"], "glob": det.get("glob", "*"),
                                   "count": counts[det["id"]]["count"], "at": _now()}
    _write_json(base_file, baseline)
    return {"detectors": {k: {"count": v["count"], "baseline": baseline[k]["count"],
                              "sites": v["sites"][:50]} for k, v in counts.items()}}


# --- patch export -------------------------------------------------------------------

def is_git_repo(repo: Path) -> bool:
    """repo/ is its own git repository (not a folder inside an enclosing one,
    which git would otherwise walk up to)."""
    return (Path(repo) / ".git").exists()


def git_diff(run: Callable, cwd: str, output: Path, base_ref: str = "HEAD") -> Any:
    """Write `git diff <base_ref>` of the working tree at `cwd` (workspace-
    relative) to the file `output`: new files included (marked
    intent-to-add first), generated trees left out, no external diff
    drivers or textconv filters. git writes the file itself, so a large
    patch is never cut like captured command output. Returns the result
    of the diff `run`."""
    spec = ["--", ".", *PATCH_EXCLUDES]
    run(["git", "add", "--intent-to-add", "--all", *spec], cwd=cwd)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    return run(["git", "diff", "--no-color", "--no-ext-diff", "--no-textconv", f"--output={output}",
                base_ref, *spec], cwd=cwd)


def export_patch(workspace: Path, *, fetch=None, run: Callable | None = None, resolve_path=None,
                 base_ref: str = "HEAD", output: str = "", path: str = "repo", **_: Any) -> dict:
    """Write `git diff <base_ref>` of repo/ (new files included) to a deliverable."""
    if run is None:
        return {"error": "command execution is not available to this tool"}
    if not re.fullmatch(r"[A-Za-z0-9._/~^][A-Za-z0-9._/~^-]*", base_ref):   # never an option
        return {"error": "invalid base_ref"}
    resolve = _resolver(workspace, resolve_path)
    target = resolve(output, write=True)
    if target.relative_to(Path(workspace).resolve()).parts[0] != "deliverables":
        return {"error": "output must be under deliverables/"}
    if not is_git_repo(resolve(path)):
        return {"error": f"{path}/ is not a git repository"}
    res = git_diff(run, path, target, base_ref)
    if res.exit_code != 0 or not target.is_file():
        return {"error": f"git diff failed: {(res.stderr or '').strip()[:400]}"}
    text = target.read_text(encoding="utf-8", errors="replace")
    audit = audit_patch_text(text)
    return {"written": output, "files": len(audit["files"]), "changed_lines": audit["changed_lines"],
            "lockfile_lines": audit["lockfile_lines"],
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def patch_record_path(workspace: Path) -> Path:
    return Path(workspace) / STATE_DIR / "patches" / "exported.json"


def load_patch_records(workspace: Path) -> dict:
    """{deliverable path: {"sha256", "base", "milestone", "at"} or {"error", ...}}
    as the specialist's finalize step recorded them."""
    try:
        data = json.loads(patch_record_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_patch(workspace: Path, deliverable: str, **fields: Any) -> None:
    records = load_patch_records(workspace)
    records[deliverable] = dict(fields, at=_now())
    _write_json(patch_record_path(workspace), records)


# --- tool table -----------------------------------------------------------------------

_DETECTOR_SCHEMA = {"type": "array", "items": {"type": "object", "required": ["id", "regex"], "properties": {
    "id": {"type": "string"}, "regex": {"type": "string"},
    "glob": {"type": "string", "description": "file glob, e.g. *.py"},
    "description": {"type": "string"}}}}

TOOL_DEFS: list[dict] = [
    {"name": "inventory_dependencies",
     "description": "Parse every lockfile and manifest under repo/ (requirements*.txt incl. hashed "
                    "pip-compile output, pyproject.toml, Pipfile, poetry.lock, uv.lock, Pipfile.lock, "
                    "package-lock.json, yarn.lock, pnpm-lock.yaml, package.json, go.mod) into one "
                    "dependency list with ecosystem, version, direct/dev flags. Dependency files of "
                    "other ecosystems are listed as 'unknown' rows (not read); name them in the plan. "
                    "Optionally writes the list as JSON to `output`.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "default": "repo"},
         "output": {"type": "string", "description": "e.g. deliverables/m1-assess/inventory.json"}}},
     "risk": "write", "function": inventory_dependencies},
    {"name": "osv_scan",
     "description": "Look up every pinned dependency version in the OSV vulnerability database "
                    "(api.osv.dev), save the answers and full advisory records as evidence, and "
                    "return the findings.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string", "default": "repo"}}},
     "risk": "network", "function": osv_scan},
    {"name": "plan_upgrades",
     "description": "From the recorded OSV evidence, compute per vulnerable package the smallest "
                    "version fixing every advisory with a known fix, the bump size, severity "
                    "(CVSS v3 base score) and advisories left open; ranked. Optionally writes "
                    "findings CSV and plan JSON.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "default": "repo"},
         "findings_csv": {"type": "string", "description": "e.g. deliverables/m1-assess/findings.csv"},
         "output": {"type": "string", "description": "e.g. deliverables/m1-assess/plan.json"}}},
     "risk": "write", "function": plan_upgrades},
    {"name": "package_versions",
     "description": "Release history of one package from PyPI or npm with each version's age in "
                    "days; versions newer than min_age_days (default: the client's policy, at least "
                    "7) are flagged too_new.",
     "input_schema": {"type": "object", "required": ["ecosystem", "name"], "properties": {
         "ecosystem": {"type": "string", "enum": ["PyPI", "npm"]}, "name": {"type": "string"},
         "min_age_days": {"type": "integer"}}},
     "risk": "network", "function": package_versions},
    {"name": "run_tests",
     "description": "Run the test command in repo/ and return exit code and pass/fail/skip counts. "
                    "Every run is logged with an id and the dependency versions it tested; only runs "
                    "of the client's exact test command in repo/ count as evidence. Cite the id of "
                    "the green run after each upgrade step in the upgrade log.",
     "input_schema": {"type": "object", "required": ["argv"], "properties": {
         "argv": {"type": "array", "items": {"type": "string"}},
         "cwd": {"type": "string", "default": "repo"},
         "label": {"type": "string", "description": "e.g. baseline, step-3 requests 2.31.0"},
         "timeout": {"type": "integer"}}},
     "risk": "exec", "function": run_tests},
    {"name": "audit_diff",
     "description": "Audit a unified diff: changed lines (lockfiles counted apart), skip/xfail/"
                    "deselect markers added, test functions removed, test files deleted, paths "
                    "outside `allow` or inside `forbid` globs.",
     "input_schema": {"type": "object", "required": ["patch"], "properties": {
         "patch": {"type": "string"}, "allow": {"type": "array", "items": {"type": "string"}},
         "forbid": {"type": "array", "items": {"type": "string"}}}},
     "risk": "read", "function": audit_diff},
    {"name": "scan_patterns",
     "description": "Count matches of old-API detector regexes in repo/ and list the sites. The "
                    "first scan of a detector id records its baseline count; run it before "
                    "changing code.",
     "input_schema": {"type": "object", "required": ["detectors"], "properties": {
         "detectors": _DETECTOR_SCHEMA, "path": {"type": "string", "default": "repo"}}},
     "risk": "write", "function": scan_patterns},
    {"name": "export_patch",
     "description": "Write the git diff of repo/ against base_ref (new files included) to a file "
                    "under deliverables/.",
     "input_schema": {"type": "object", "required": ["output"], "properties": {
         "output": {"type": "string", "description": "e.g. deliverables/m2-upgrade/repo.patch"},
         "base_ref": {"type": "string", "default": "HEAD"}}},
     "risk": "exec", "function": export_patch},
]
