"""
specialists/appsec_review/tools.py - deterministic domain tools for the
application security review specialist.

Every heavy, verifiable step lives here as a plain function rather than in the
model's head: the secret scanner, the OSV dependency audit, the OpenAPI
attack-surface parser, the SARIF 2.1.0 writer, the CVSS v3.1 calculator and the
scope allow-list guard. Tools take the shared signature
`fn(workspace: Path, *, fetch=None, run=None, **args) -> dict | str` and use the
injected `fetch` for network work (never urllib/requests directly), so egress
stays policy-checked and tests stay offline. TOOL_DEFS at the bottom is what the
kit registers with the model.

Under the kit, model-supplied paths go through the injected `resolve_path`
(workspace jail, inputs/ read-only, .agentkit/ refused, and files written here
are marked agent-authored so the checks never accept them as customer source).
Called directly (unit tests, the acceptance checks) the tools fall back to a
plain workspace jail. audit_dependencies also receives the claim `ledger` and
registers every OSV answer that lists a vulnerability as a source, so advisory
facts can be cited like any other claim.

The acceptance checks (checks.py) reuse the same helpers - find_secrets,
load_scope, scope_verdict, cvss_base_score - so a check recomputes exactly
what the tool computed instead of trusting the agent's copy of it.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from agentkit.errors import PolicyViolation, ToolError
from agentkit.policy import INTERNAL_DIR, jail_path

# --- workspace helpers -------------------------------------------------------

# Directories never walked: VCS metadata and vendored or generated trees.
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "bower_components", ".venv", "venv",
              "__pycache__", "dist", "build", ".mypy_cache", ".pytest_cache", ".tox",
              "vendor", ".idea"}
# Suffixes that are never text. Every other file is opened and sniffed, so
# .pem, .key, .env.production, .tfvars, .kt and the like are all scanned.
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".pdf",
                    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".ear",
                    ".class", ".so", ".dll", ".exe", ".dylib", ".o", ".a", ".lib", ".pyc", ".pyo",
                    ".whl", ".egg", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
                    ".mov", ".avi", ".wav", ".sqlite", ".sqlite3", ".db", ".bin"}
MAX_SCAN_BYTES = 2_000_000
MAX_LINE_CHARS = 4000        # longer lines are minified blobs
MAX_SKIPPED_LISTED = 50


def _resolve(workspace: Path, rel: str, resolve_path: Callable | None = None, *,
             write: bool = False) -> Path:
    """A workspace path for `rel`: through the kit's resolve_path when the tool
    runs under the kit, else a plain jail. Escapes raise ToolError here and
    PolicyViolation under the kit (reported to the model as a denial)."""
    if resolve_path is not None:
        return resolve_path(rel, write=write)
    try:
        return jail_path(Path(workspace), rel)
    except PolicyViolation as exc:
        raise ToolError(f"path escapes workspace: {rel} ({exc})") from None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def text_files(root: Path, workspace: Path) -> tuple[list[Path], list[dict]]:
    """Text files under root, plus what was skipped and why. Kit internals are
    left out silently; anything that resolves outside the workspace (a
    symlink), binary content, oversized files and vendored/VCS directories are
    reported as skipped so a coverage gap is visible."""
    ws = Path(workspace).resolve()
    files: list[Path] = []
    skipped: list[dict] = []

    def rel(path: Path) -> str:
        try:
            return path.relative_to(ws).as_posix()
        except ValueError:
            return path.as_posix()

    def internal(path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(ws).parts
        except (OSError, ValueError):
            return False
        return bool(parts) and parts[0] == INTERNAL_DIR

    def consider(path: Path) -> None:
        try:
            real = path.resolve()
        except OSError:
            skipped.append({"file": rel(path), "reason": "unreadable"})
            return
        if not real.is_relative_to(ws):
            skipped.append({"file": rel(path), "reason": "links outside the workspace"})
            return
        if internal(path):
            return
        if path.suffix.lower() in _BINARY_SUFFIXES:
            skipped.append({"file": rel(path), "reason": "binary file type"})
            return
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                skipped.append({"file": rel(path), "reason": "larger than 2 MB"})
                return
            with path.open("rb") as fh:
                head = fh.read(8192)
        except OSError:
            skipped.append({"file": rel(path), "reason": "unreadable"})
            return
        if b"\x00" in head:
            skipped.append({"file": rel(path), "reason": "binary content"})
            return
        files.append(path)

    if root.is_file():
        consider(root)
        return files, skipped
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if internal(here / d):
                continue
            if (here / d).is_symlink():
                skipped.append({"file": rel(here / d) + "/", "reason": "symlinked directory not followed"})
                continue
            if d in _SKIP_DIRS:
                skipped.append({"file": rel(here / d) + "/", "reason": "vendored, generated or VCS directory"})
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            consider(here / name)
    return files, skipped


def redact_secret(secret: str) -> str:
    """Keep a fingerprint, hide the body: 'AKIA***34 (len 20)', never the value."""
    secret = secret.strip()
    if len(secret) <= 8:
        return secret[:2] + "***"
    return f"{secret[:4]}***{secret[-2:]} (len {len(secret)})"


# --- secret scanning ---------------------------------------------------------

# In-house patterns, written from first principles (clean-room): each is
# shaped to the token's publicly documented format, not copied from any
# third-party rule set. Group 1 is the secret value.
_KEYWORD = (r"(?:passw(?:or)?d|passphrase|secret|token|api[_-]?key|access[_-]?key"
            r"|private[_-]?key|auth[_-]?key|signing[_-]?key)")
# The keyword may sit anywhere in an identifier or key: SECRET_KEY,
# db_password, client_secret, GITHUB_TOKEN, "apiKey" (a JSON key), db.password.
_IDENT = rf"(?<![\w.-])[\w.-]*{_KEYWORD}[\w.-]*"
_ASSIGN = r"[\"']?\s*(?::=|=>|[:=])\s*"


class _Rule:
    def __init__(self, rule_id: str, name: str, cwe: str, severity: str, pattern: str, *,
                 generic: bool = False, config_only: bool = False):
        self.rule_id, self.name, self.cwe, self.severity = rule_id, name, cwe, severity
        self.pattern = re.compile(pattern)
        self.generic = generic            # dropped where a specific rule hit the same value
        self.config_only = config_only    # unquoted values only count in config files


_RULES = [
    _Rule("SECRET.AWS_ACCESS_KEY_ID", "AWS access key id", "CWE-798", "high",
          r"\b((?:AKIA|ASIA|AIDA|AGPA)[A-Z0-9]{16})\b"),
    _Rule("SECRET.PRIVATE_KEY_BLOCK", "Private key block", "CWE-798", "critical",
          r"(-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----)"),
    _Rule("SECRET.GITHUB_TOKEN", "GitHub token", "CWE-798", "high",
          r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b"),
    _Rule("SECRET.STRIPE_LIVE_KEY", "Stripe live secret key", "CWE-798", "critical",
          r"\b((?:sk|rk)_live_[A-Za-z0-9]{16,})\b"),
    _Rule("SECRET.GOOGLE_API_KEY", "Google API key", "CWE-798", "high",
          r"\b(AIza[0-9A-Za-z_-]{35})(?![0-9A-Za-z_-])"),
    _Rule("SECRET.SLACK_TOKEN", "Slack token", "CWE-798", "high",
          r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    _Rule("SECRET.JWT", "JSON Web Token", "CWE-522", "medium",
          r"\b(ey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"),
    _Rule("SECRET.URL_CREDENTIALS", "Credentials in a connection URL", "CWE-798", "high",
          r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@\"'<>]+:([^\s/@\"'<>]{3,})@[^\s\"'<>]+"),
    _Rule("SECRET.GENERIC_ASSIGNMENT", "Hard-coded credential assignment", "CWE-798", "high",
          rf"(?i){_IDENT}{_ASSIGN}[\"']([^\"'\s]{{8,}})[\"']", generic=True),
    _Rule("SECRET.GENERIC_ASSIGNMENT", "Hard-coded credential assignment", "CWE-798", "high",
          rf"(?i){_IDENT}{_ASSIGN}([^\s\"'#,;]{{8,}})\s*(?:[#;].*)?$", generic=True, config_only=True),
]
SECRET_RULE_IDS = sorted({r.rule_id for r in _RULES})

# Values that are obviously placeholders or indirections are not findings.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{3,}|\*{3,}|\.{3,}|<|\{|\$|%\(|%[a-z_]+%|your[_-]?|changeme|change[_-]me|example"
    r"|placeholder|dummy|sample|todo|redacted|(?:none|null|nil|true|false|password|passwd|pass"
    r"|secret|token)$|env\.|env\[|process\.env|os\.environ|os\.getenv|getenv\(|config\."
    r"|settings\.|vault:|arn:aws:secretsmanager)")
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=]{16,}$")


def _is_placeholder(value: str) -> bool:
    v = value.strip()
    if _PLACEHOLDER.match(v):
        return True
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", v) and "@" not in v:
        return True          # a plain URL, e.g. an OAuth token endpoint
    return v.startswith(("/", "./", "../", "~/"))   # a file path


def _is_config(path: Path) -> bool:
    """Files where an unquoted `key: value` / `KEY=value` is a literal."""
    name = path.name.lower()
    return (name.startswith(".env") or name.endswith(".env") or name == "dockerfile"
            or path.suffix.lower() in {".yaml", ".yml", ".properties", ".ini", ".cfg", ".conf"})


def _line_hits(lines: list[str], idx: int, config: bool) -> list[dict]:
    """Secret hits on lines[idx], each with its raw `value` and the `probe`
    (the part that must never appear in a deliverable)."""
    line = lines[idx]
    if len(line) > MAX_LINE_CHARS:
        return []
    hits: list[dict] = []
    for rule in _RULES:
        if rule.config_only and not config:
            continue
        for m in rule.pattern.finditer(line):
            value = m.group(1)
            if _is_placeholder(value):
                continue
            probe = value
            if rule.rule_id == "SECRET.PRIVATE_KEY_BLOCK":
                body = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
                probe = body if _BASE64ISH.match(body) else ""
            hits.append({"rule": rule, "column": m.start() + 1, "span": m.span(1),
                         "value": value, "probe": probe})
    specific = [h["span"] for h in hits if not h["rule"].generic]
    return [h for h in hits
            if not (h["rule"].generic and any(a < h["span"][1] and h["span"][0] < b for a, b in specific))]


def find_secrets(workspace: Path, root: str = "repo") -> tuple[list[dict], int, list[dict]]:
    """(hits, files_scanned, skipped) for text files under workspace/root.
    Each hit carries the raw `value` and `probe`: for the acceptance checks
    only, never returned to the model. Raises ToolError on a path escape."""
    ws = Path(workspace).resolve()
    base = _resolve(ws, root)
    if not base.exists():
        return [], 0, []
    files, skipped = text_files(base, ws)
    hits: list[dict] = []
    for path in files:
        rel = path.resolve().relative_to(ws).as_posix()
        lines = _read_text(path).splitlines()
        config = _is_config(path)
        for idx in range(len(lines)):
            for h in _line_hits(lines, idx, config):
                rule = h["rule"]
                hits.append({"rule_id": rule.rule_id, "name": rule.name, "file": rel, "line": idx + 1,
                             "column": h["column"], "cwe": rule.cwe, "severity": rule.severity,
                             "value": h["value"], "probe": h["probe"]})
    hits.sort(key=lambda f: (f["file"], f["line"], f["column"], f["rule_id"]))
    return hits, len(files), skipped


def _public_hit(hit: dict) -> dict:
    return {"rule_id": hit["rule_id"], "name": hit["name"], "file": hit["file"], "line": hit["line"],
            "column": hit["column"], "excerpt": redact_secret(hit["value"]), "cwe": hit["cwe"],
            "severity": hit["severity"]}


def scan_secrets(workspace: Path, *, fetch=None, run=None, root: str = "repo",
                 resolve_path: Callable | None = None, **_: Any) -> dict:
    """Scan every text file under `root` for hard-coded secrets.

    Returns the hit count, files scanned, the findings (file workspace-relative,
    1-based line/column, the rule that hit, a redacted excerpt, CWE, severity)
    and the files or directories skipped with the reason. The matched secret is
    never returned in full.
    """
    base = _resolve(workspace, root, resolve_path)
    ws = Path(workspace).resolve()
    hits, scanned, skipped = find_secrets(ws, base.relative_to(ws).as_posix() or ".")
    return {"secret_count": len(hits), "files_scanned": scanned, "skipped_count": len(skipped),
            "findings": [_public_hit(h) for h in hits], "skipped": skipped[:MAX_SKIPPED_LISTED]}


def _secret_values_near(workspace: Path, code_root: str, file: str, line: int) -> list[str]:
    """Raw secret values the scanner sees on lines line-1..line+1 of a code
    file (used to scrub them from a SARIF result's text)."""
    try:
        path = jail_path(Path(workspace), f"{code_root.strip('/') or '.'}/{file}")
    except PolicyViolation:
        return []
    if not path.is_file():
        return []
    lines = _read_text(path).splitlines()
    out: list[str] = []
    for idx in range(max(0, line - 2), min(len(lines), line + 1)):
        for h in _line_hits(lines, idx, _is_config(path)):
            if h["probe"] and len(h["probe"]) >= 4:
                out.append(h["probe"])
    return out


# --- dependency audit (OSV) --------------------------------------------------

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*===?\s*([A-Za-z0-9_.!+-]+)$")
_PUBLIC_NPM = ("registry.npmjs.org", "registry.yarnpkg.com")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """requirements.txt lines with backslash continuations joined."""
    out, buf, start = [], "", 0
    for n, raw in enumerate(text.splitlines(), start=1):
        if not buf:
            start = n
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append((start, buf + raw))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def _parse_requirements(text: str) -> tuple[list[dict], list[dict], list[str]]:
    """requirements.txt -> (pinned packages, unaudited lines with the reason,
    index URLs). Extras, environment markers and --hash options are stripped;
    ranges, unpinned names, includes, editables and URLs are not guessed at."""
    pkgs: list[dict] = []
    unaudited: list[dict] = []
    indexes: list[str] = []
    for lineno, raw in _logical_lines(text):
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        if not line:
            continue

        def skip(reason: str) -> None:
            unaudited.append({"line": lineno, "text": line[:200], "reason": reason})

        if line.startswith("-"):
            opt = line.split()[0].split("=", 1)[0]
            if opt in ("-r", "--requirement", "-c", "--constraint") or re.match(r"^-[rc]\S", opt):
                skip("included file not followed: audit it separately")
            elif opt in ("-e", "--editable"):
                skip("editable or local install")
            elif opt in ("-i", "--index-url", "--extra-index-url"):
                indexes.append(line)
            continue
        spec = line.split(";", 1)[0].strip()                     # environment marker
        spec = re.sub(r"\s+--hash[=\s]\S+", "", spec).strip()    # hash-checking mode
        if "://" in spec or " @ " in spec or spec.startswith((".", "/")):
            skip("URL or local path, not a registry version")
            continue
        m = _PIN.match(spec)
        if m:
            pkgs.append({"name": m.group(1), "version": m.group(2), "ecosystem": "PyPI"})
        elif re.search(r"[<>~!=*]", spec):
            skip("version range, not a pinned version")
        else:
            skip("no version pinned")
    return pkgs, unaudited, indexes


def _parse_package_lock(text: str) -> tuple[list[dict], list[dict]]:
    """npm package-lock.json (v2/v3 `packages` map, v1 `dependencies`) ->
    (pinned dependencies, unaudited entries). A package resolved from a
    registry other than the public npm one is private: it is listed, never
    sent to OSV."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"package-lock.json is not valid JSON: {exc}") from None
    pkgs: list[dict] = []
    unaudited: list[dict] = []

    def add(name: str, meta: Any) -> None:
        if isinstance(meta, str):       # package.json: a range, not what is installed
            unaudited.append({"package": name, "reason": "version range in package.json; "
                                                         "audit package-lock.json instead"})
            return
        meta = meta if isinstance(meta, dict) else {}
        version, resolved = meta.get("version"), str(meta.get("resolved") or "")
        if meta.get("link") or not version:
            unaudited.append({"package": name, "reason": "linked or unversioned package"})
        elif resolved.startswith("http") and not any(h in resolved for h in _PUBLIC_NPM):
            unaudited.append({"package": name, "reason": "resolved from a private registry; not sent to OSV"})
        else:
            pkgs.append({"name": name, "version": str(version), "ecosystem": "npm"})

    if not isinstance(data, dict):
        raise ToolError("package-lock.json must be a JSON object")
    for key, meta in (data.get("packages") or {}).items():
        if key and "node_modules/" in f"{key}/":
            add(key.split("node_modules/")[-1], meta)
    for name, meta in (data.get("dependencies") or {}).items():
        add(name, meta)

    def unique(items: list[dict]) -> list[dict]:
        seen, out = set(), []
        for item in items:
            k = json.dumps(item, sort_keys=True)
            if k not in seen:
                seen.add(k)
                out.append(item)
        return out

    return unique(pkgs), unique(unaudited)


_PRE_LABELS = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "c": "rc", "rc": "rc",
               "pre": "rc", "preview": "rc"}


def _version_key(version: str, ecosystem: str = "PyPI") -> tuple | None:
    """A sortable key for PEP 440 (PyPI) or semver (npm) versions, close
    enough to order OSV range events: epoch, release (trailing zeros
    dropped), then dev < pre-release < final < post. None if unparseable
    (e.g. a git hash)."""
    s = str(version).strip().lower()
    s = s[1:] if s.startswith("v") else s
    s = s.split("+", 1)[0]
    epoch = 0
    if "!" in s:
        e, s = s.split("!", 1)
        if not e.isdigit():
            return None
        epoch = int(e)
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", s)
    if not m:
        return None
    release = [int(x) for x in m.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    rest = m.group(2).strip(".-_")
    if not rest:
        tail: tuple = (2, "", 0)
    else:
        t = re.match(r"^([a-z]*)[.\-_]?(\d*)", rest)
        label, num = t.group(1), int(t.group(2) or 0)
        if ecosystem.lower() == "npm":
            tail = (1, _PRE_LABELS.get(label, label), num)       # any semver suffix is a pre-release
        elif label in ("post", "rev", "r") or (not label and num):
            tail = (3, "", num)
        elif label == "dev":
            tail = (0, "", num)
        else:
            tail = (1, _PRE_LABELS.get(label, label), num)
    return (epoch, tuple(release), tail)


_BOTTOM = (-1, (), (0, "", 0))    # OSV "introduced": "0"


def _norm_pkg(name: str, ecosystem: str) -> str:
    name = str(name or "")
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem.lower() == "pypi" else name.lower()


def _osv_fix(vuln: dict, pkg: dict) -> tuple[str, list[str]]:
    """(fixed, candidates): the fix for the range that contains the installed
    version (never a lower branch's fix), else the smallest listed fix above
    it, else ''; plus every fixed version OSV lists for the package."""
    eco = pkg["ecosystem"]
    installed = _version_key(pkg["version"], eco)
    want = _norm_pkg(pkg["name"], eco)
    candidates: set[str] = set()
    fix: str | None = None
    for aff in vuln.get("affected") or []:
        p = aff.get("package") or {}
        if _norm_pkg(p.get("name", ""), eco) != want:
            continue
        if p.get("ecosystem") and str(p["ecosystem"]).lower() != eco.lower():
            continue
        for rng in aff.get("ranges") or []:
            if str(rng.get("type", "")).upper() not in ("ECOSYSTEM", "SEMVER"):
                continue
            events = []
            for ev in rng.get("events") or []:
                for kind, order in (("fixed", 0), ("last_affected", 0), ("limit", 0), ("introduced", 1)):
                    if kind not in ev:
                        continue
                    raw = str(ev[kind])
                    key = _BOTTOM if (kind == "introduced" and raw == "0") else _version_key(raw, eco)
                    if key is None:
                        continue
                    if kind == "fixed":
                        candidates.add(raw)
                    events.append((key, order, kind, raw))
            opened = None
            for key, _, kind, raw in sorted(events):
                if kind == "introduced":
                    opened = key if opened is None else opened
                    continue
                if opened is None:
                    continue
                inside = installed is not None and opened <= installed and (
                    installed <= key if kind == "last_affected" else installed < key)
                if inside and fix is None:
                    fix = raw if kind == "fixed" else ""
                opened = None
            if opened is not None and installed is not None and opened <= installed and fix is None:
                fix = ""            # affected with no fix yet on this branch
    ordered = sorted(candidates, key=lambda v: _version_key(v, eco) or _BOTTOM)
    if fix is None:
        above = [v for v in ordered if installed is not None and (_version_key(v, eco) or _BOTTOM) > installed]
        fix = above[0] if above else ""
    return fix, ordered


def _osv_severity(vuln: dict) -> tuple[str, list[dict]]:
    """(label, vectors): a coarse severity label (the database's own, else the
    band of its CVSS v3 vector, else UNKNOWN) and the CVSS vectors OSV gives,
    v3 first, each with its type so a v2 or v4 vector is never mistaken for v3."""
    rank = {"CVSS_V3": 0, "CVSS_V4": 1, "CVSS_V2": 2}
    vectors = sorted(({"type": str(s.get("type", "")), "score": str(s.get("score", ""))}
                      for s in vuln.get("severity") or [] if isinstance(s, dict) and s.get("score")),
                     key=lambda v: rank.get(v["type"], 3))
    label = str((vuln.get("database_specific") or {}).get("severity") or "").upper()
    if not label:
        for v in vectors:
            if v["type"] == "CVSS_V3":
                try:
                    label = cvss_base_score(vector=v["score"])["severity"].upper()
                except ToolError:
                    pass
                break
    return label or "UNKNOWN", vectors


def _excluded(name: str, eco: str, patterns: list[str]) -> bool:
    key = _norm_pkg(name, eco)
    for pat in patterns:
        p = _norm_pkg(str(pat).strip(), eco)
        if p.endswith("*") and key.startswith(p[:-1]):
            return True
        if key == p:
            return True
    return False


def audit_dependencies(workspace: Path, *, fetch: Callable | None = None, run=None,
                       manifest: str = "repo/requirements.txt",
                       output_path: str = "deliverables/m2-findings/dependencies.json",
                       exclude: list[str] | None = None,
                       resolve_path: Callable | None = None, ledger: Any = None,
                       **_: Any) -> dict:
    """Parse a dependency manifest and query OSV for known vulnerabilities.

    Sends each pinned package name and version (nothing else) to the OSV query
    API through the injected `fetch`; packages matching `exclude` (names, or
    prefixes ending in '*', e.g. internal ones) and npm packages from a private
    registry are never sent. The manifest may be a requirements.txt or an npm
    package-lock.json. Returns the advisories first, then the lines or packages
    that were not audited and why; the complete audit (every package) is
    written to `output_path`. With a `ledger`, each OSV answer that lists a
    vulnerability is registered as a source (kind "tool") whose id is the
    vulnerability's `source`.
    """
    path = _resolve(workspace, manifest, resolve_path)
    if not path.is_file():
        raise ToolError(f"manifest not found: {manifest}")
    out = _resolve(workspace, output_path, resolve_path, write=True) if output_path else None
    text = _read_text(path)
    indexes: list[str] = []
    if path.suffix.lower() == ".json":
        packages, unaudited = _parse_package_lock(text)
    else:
        packages, unaudited, indexes = _parse_requirements(text)
    if fetch is None:
        raise ToolError("audit_dependencies needs network access (fetch); "
                        "OSV egress must be on the allow-list")

    patterns = [str(p) for p in (exclude or [])]
    vulnerabilities: list[dict] = []
    audited: list[dict] = []
    for pkg in packages:
        if _excluded(pkg["name"], pkg["ecosystem"], patterns):
            unaudited.append({"package": pkg["name"], "reason": "listed as internal; not sent to OSV"})
            continue
        body = json.dumps({"version": pkg["version"],
                           "package": {"name": pkg["name"], "ecosystem": pkg["ecosystem"]}})
        resp = fetch("https://api.osv.dev/v1/query", method="POST",
                     headers={"Content-Type": "application/json"}, body=body)
        status = getattr(resp, "status", 200)
        if status != 200:
            raise ToolError(f"OSV query failed for {pkg['name']}: HTTP {status}")
        answer = getattr(resp, "text", "") or "{}"
        try:
            vulns = json.loads(answer).get("vulns", []) or []
        except (json.JSONDecodeError, AttributeError):
            raise ToolError(f"OSV answer for {pkg['name']} is not a JSON object") from None
        audited.append(pkg)
        source_id = ""
        if vulns and ledger is not None:
            source_id = ledger.add_source(
                f"osv:{pkg['ecosystem']}/{pkg['name']}@{pkg['version']}",
                f"OSV advisories for {pkg['name']} {pkg['version']} ({pkg['ecosystem']})",
                answer, kind="tool").id
        for vuln in vulns:
            label, vectors = _osv_severity(vuln)
            fixed, candidates = _osv_fix(vuln, pkg)
            vulnerabilities.append({
                "package": pkg["name"], "version": pkg["version"], "ecosystem": pkg["ecosystem"],
                "id": vuln.get("id", ""), "aliases": vuln.get("aliases", []) or [],
                "summary": (vuln.get("summary") or vuln.get("details") or "")[:200],
                "severity": label, "cvss": vectors,
                "fixed": fixed, "fixed_candidates": candidates, "source": source_id,
            })
    vulnerabilities.sort(key=lambda v: (v["package"], v["id"]))
    summary = {
        "manifest": manifest,
        "package_count": len(packages),
        "audited_count": len(audited),
        "vulnerable_count": len({v["package"] for v in vulnerabilities}),
        "advisory_count": len(vulnerabilities),
        "vulnerabilities": vulnerabilities,
        "unaudited": unaudited,
        "index_urls": indexes,
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({**summary, "packages": packages}, indent=2), encoding="utf-8")
        summary["full_report"] = output_path
    return summary


# --- OpenAPI / attack surface ------------------------------------------------

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def load_spec(path: Path) -> dict:
    """An OpenAPI/Swagger document from JSON or YAML (safe_load only)."""
    text = _read_text(path)
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ToolError(f"spec is neither JSON nor YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ToolError("spec must be a JSON/YAML object")
    return data


def _deref(spec: dict, node: Any, depth: int = 0) -> Any:
    """Follow a local `$ref` ('#/components/parameters/x'); anything else as is."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 10:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return node
        target: Any = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return node
            target = target[part]
        node, depth = target, depth + 1
    return node


def parse_openapi(workspace: Path, *, fetch=None, run=None,
                  spec_path: str = "inputs/openapi.json",
                  resolve_path: Callable | None = None, **_: Any) -> dict:
    """Enumerate endpoints from an OpenAPI/Swagger spec (JSON or YAML).

    Returns every operation with its method, path, operationId, whether auth is
    required (an empty security requirement `{}` means anonymous access is
    allowed, so such routes count as unauthenticated) and its parameter names
    (local $refs resolved), plus an attack-surface summary: total endpoints,
    unauthenticated endpoints and declared servers.
    """
    path = _resolve(workspace, spec_path, resolve_path)
    if not path.is_file():
        raise ToolError(f"spec not found: {spec_path}")
    spec = load_spec(path)
    global_security = spec.get("security")
    servers = [s.get("url", "") for s in spec.get("servers", []) or [] if isinstance(s, dict)]
    if not servers and spec.get("host"):  # swagger 2.0
        scheme = (spec.get("schemes") or ["https"])[0]
        servers = [f"{scheme}://{spec['host']}{spec.get('basePath', '')}"]

    endpoints: list[dict] = []
    unresolved: set[str] = set()
    for route, item in (spec.get("paths") or {}).items():
        item = _deref(spec, item)
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters", []) or []
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            sec = op["security"] if "security" in op else global_security
            sec = sec if isinstance(sec, list) else []
            auth_required = bool(sec) and not any(isinstance(r, dict) and not r for r in sec)
            names: set[str] = set()
            has_body = "requestBody" in op
            for p in list(shared_params) + list(op.get("parameters", []) or []):
                resolved = _deref(spec, p)
                if not isinstance(resolved, dict):
                    continue
                if resolved.get("name"):
                    names.add(str(resolved["name"]))
                elif "$ref" in resolved:
                    unresolved.add(str(resolved["$ref"]))
                has_body = has_body or resolved.get("in") == "body"
            endpoints.append({
                "method": method.upper(),
                "path": route,
                "operation_id": op.get("operationId", ""),
                "auth_required": auth_required,
                "parameters": sorted(names),
                "accepts_body": has_body,
            })
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    unauth = [f"{e['method']} {e['path']}" for e in endpoints if not e["auth_required"]]
    out = {"endpoints": endpoints, "count": len(endpoints),
           "unauthenticated": unauth, "servers": servers}
    if unresolved:
        out["unresolved_refs"] = sorted(unresolved)
    return out


# --- CVSS v3.1 base score ----------------------------------------------------

_CVSS_METRICS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_PR = {"U": {"N": 0.85, "L": 0.62, "H": 0.27},
       "C": {"N": 0.85, "L": 0.68, "H": 0.50}}
_BASE_METRICS = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
# Temporal and environmental metrics are valid in a vector but do not change
# the base score.
_OTHER_METRICS = {"E", "RL", "RC", "CR", "IR", "AR", "MAV", "MAC", "MPR", "MUI", "MS", "MC", "MI", "MA"}


def _cvss_roundup(value: float) -> float:
    """CVSS v3.1 Roundup: smallest 1-decimal number >= value (spec Appendix A)."""
    scaled = round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (math.floor(scaled / 10000) + 1) / 10.0


def _cvss_severity(score: float) -> str:
    if score == 0.0:
        return "None"
    if score < 4.0:
        return "Low"
    if score < 7.0:
        return "Medium"
    if score < 9.0:
        return "High"
    return "Critical"


def sarif_level(score: float) -> str:
    """The SARIF level for a CVSS base score (the playbook's band table)."""
    if score >= 7.0:
        return "error"
    if score >= 4.0:
        return "warning"
    return "note" if score > 0 else "none"


def cvss_base_score(workspace: Path | None = None, *, fetch=None, run=None,
                    vector: str = "", **_: Any) -> dict:
    """Compute a CVSS v3.x base score and severity from a vector string.

    Example vector: 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'. The
    'CVSS:3.1/' (or 'CVSS:3.0/') prefix is required, every base metric must
    appear exactly once, and unknown metrics are refused, so a mistyped vector
    raises ToolError instead of carrying a confident, wrong score. 3.0
    vectors share the base equations and are scored with the 3.1 Roundup.
    """
    if not isinstance(vector, str) or not vector.strip():
        raise ToolError("cvss_base_score needs a vector string")
    tokens = vector.strip().split("/")
    version = tokens[0].upper()
    if version not in ("CVSS:3.1", "CVSS:3.0"):
        raise ToolError(f"CVSS vector must start with CVSS:3.1/ (or CVSS:3.0/): {vector}")
    parts: dict[str, str] = {}
    for token in tokens[1:]:
        key, sep, val = token.partition(":")
        key, val = key.strip().upper(), val.strip().upper()
        if not sep or not key or not val:
            raise ToolError(f"CVSS vector has a malformed part {token!r}: {vector}")
        if key not in _BASE_METRICS and key not in _OTHER_METRICS:
            raise ToolError(f"CVSS vector has an unknown metric {key!r}: {vector}")
        if key in parts:
            raise ToolError(f"CVSS vector repeats metric {key!r}: {vector}")
        parts[key] = val
    scope = parts.get("S")
    if scope not in ("U", "C"):
        raise ToolError(f"CVSS vector missing/invalid scope (S): {vector}")
    try:
        av = _CVSS_METRICS["AV"][parts["AV"]]
        ac = _CVSS_METRICS["AC"][parts["AC"]]
        ui = _CVSS_METRICS["UI"][parts["UI"]]
        pr = _PR[scope][parts["PR"]]
        c = _CVSS_METRICS["C"][parts["C"]]
        i = _CVSS_METRICS["I"][parts["I"]]
        a = _CVSS_METRICS["A"][parts["A"]]
    except KeyError as exc:
        raise ToolError(f"CVSS vector missing/invalid metric {exc}: {vector}") from None

    iss = 1 - ((1 - c) * (1 - i) * (1 - a))
    if scope == "U":
        impact = 6.42 * iss
    else:
        impact = 7.52 * (iss - 0.029) - 3.25 * ((iss - 0.02) ** 15)
    exploitability = 8.22 * av * ac * pr * ui
    if impact <= 0:
        score = 0.0
    elif scope == "U":
        score = _cvss_roundup(min(impact + exploitability, 10.0))
    else:
        score = _cvss_roundup(min(1.08 * (impact + exploitability), 10.0))
    return {"vector": vector, "version": version.split(":")[1], "base_score": score,
            "severity": _cvss_severity(score)}


# --- scope allow-list guard --------------------------------------------------

def host_of(target: str) -> str:
    """Bare host from a URL or host string, port and path stripped."""
    t = str(target).strip()
    t = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", t)  # drop scheme
    t = t.split("/", 1)[0].split("@")[-1]               # drop path, userinfo
    if t.startswith("["):                               # [v6]:port
        return t[1:].split("]", 1)[0].lower()
    return t.split(":", 1)[0].lower().rstrip(".")       # drop port


def _host_rule(entry: Any) -> str | None:
    """A normalized host rule ('host', '*.domain', or an IP/CIDR) from a
    scope entry, or None when the entry is prose."""
    if not isinstance(entry, str) or not entry.strip() or " " in entry.strip():
        return None
    raw = entry.strip().lower()
    try:
        return str(ipaddress.ip_network(raw, strict=False))
    except ValueError:
        pass
    host = ("*." if raw.startswith("*.") else "") + host_of(raw[2:] if raw.startswith("*.") else raw)
    bare = host[2:] if host.startswith("*.") else host
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", bare):
        return host
    return None


def host_matches(host: str, rule: str) -> bool:
    """Exact rules match only that host; '*.dom' matches subdomains of dom
    (not dom itself); an IP/CIDR rule matches addresses inside it."""
    host = host.lower().rstrip(".")
    if rule.startswith("*."):
        return host.endswith("." + rule[2:])
    if "/" in rule:
        try:
            return ipaddress.ip_address(host) in ipaddress.ip_network(rule)
        except ValueError:
            return False
    return host == rule


def _repo_key(url: str) -> str:
    key = re.sub(r"^[a-z][a-z0-9+.-]*://", "", str(url).strip().lower())
    key = key.split("@")[-1] if "@" in key.split("/", 1)[0] else key
    key = key.rstrip("/")
    return key[:-4] if key.endswith(".git") else key


def load_scope(workspace: Path, scope_file: str = "inputs/scope.json",
               resolve_path: Callable | None = None) -> dict:
    """The signed scope: allow rules ('hosts'), deny rules ('out_of_scope'),
    repository URLs ('repos'/'repositories') and the out-of-scope entries
    that are prose (for a human to apply)."""
    path = _resolve(workspace, scope_file, resolve_path)
    if not path.is_file():
        raise ToolError(f"scope file not found: {scope_file}")
    try:
        data = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ToolError(f"scope file is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ToolError("scope file must be a JSON object")
    for key in ("hosts", "out_of_scope", "repos", "repositories"):
        if data.get(key) is not None and not isinstance(data[key], list):
            raise ToolError(f"scope file: {key!r} must be a list")
    allow = [r for r in map(_host_rule, data.get("hosts") or []) if r]
    deny, prose = [], []
    for entry in data.get("out_of_scope") or []:
        rule = _host_rule(entry)
        (deny if rule else prose).append(rule or str(entry))
    repos = [_repo_key(r) for r in (data.get("repos") or data.get("repositories") or [])
             if isinstance(r, str) and r.strip()]
    return {"allow": allow, "deny": deny, "repos": repos, "unparsed_out_of_scope": prose}


def scope_verdict(target: str, scope: dict) -> tuple[bool, str]:
    """(in_scope, reason). Out-of-scope entries win over any allow rule."""
    host = host_of(target)
    if any(host_matches(host, r) for r in scope["deny"]):
        return False, "listed out of scope"
    if any(host_matches(host, r) for r in scope["allow"]):
        return True, "matches allow-list"
    key = _repo_key(target)
    if any(key == r or key.startswith(r + "/") for r in scope["repos"]):
        return True, "matches an in-scope repository"
    return False, "not in allow-list"


def check_scope(workspace: Path, *, fetch=None, run=None, targets: list[str] | None = None,
                scope_file: str = "inputs/scope.json",
                resolve_path: Callable | None = None, **_: Any) -> dict:
    """Decide whether each target is inside the signed scope.

    The scope file holds `hosts` (exact hosts, `*.dom` for subdomains of dom,
    or IPs/CIDRs), optional `out_of_scope` entries, which always win, and
    optional `repos`. Returns a per-target verdict and an `all_in_scope` flag
    so the agent can refuse out-of-scope work.
    """
    scope = load_scope(workspace, scope_file, resolve_path)
    results = []
    for target in (targets or []):
        ok, reason = scope_verdict(target, scope)
        results.append({"target": target, "host": host_of(target), "in_scope": ok, "reason": reason})
    return {"results": results, "allow": scope["allow"], "out_of_scope": scope["deny"],
            "repos": scope["repos"], "unparsed_out_of_scope": scope["unparsed_out_of_scope"],
            "all_in_scope": all(r["in_scope"] for r in results) if results else True}


# --- SARIF 2.1.0 writer ------------------------------------------------------

_SARIF_LEVELS = {"critical": "error", "high": "error", "medium": "warning",
                 "low": "note", "none": "none", "error": "error",
                 "warning": "warning", "note": "note"}
_BANDS = ("critical", "high", "medium", "low", "none")
_SARIF_SCHEMA = ("https://raw.githubusercontent.com/oasis-tcs/sarif-spec/"
                 "master/Schemata/sarif-schema-2.1.0.json")
_TEXT_FIELDS = ("name", "message", "description", "exploit", "remediation")


def _artifact_uri(file: Any, code_root: str) -> str:
    """SARIF artifact URI relative to the code root (what code scanning and
    sarif_locations_exist expect): a workspace path such as repo/app.py, as
    scan_secrets reports it, becomes app.py."""
    uri = str(file).replace("\\", "/")
    root = (code_root or "").replace("\\", "/").strip("/")
    if root and root != "." and uri.startswith(root + "/"):
        uri = uri[len(root) + 1:]
    return uri


def _cwe_tags(cwe: Any) -> list[str]:
    text = " ".join(cwe) if isinstance(cwe, list) else str(cwe or "")
    return [f"external/cwe/cwe-{n}" for n in dict.fromkeys(re.findall(r"(?i)CWE-(\d+)", text))]


def build_sarif(workspace: Path, *, fetch=None, run=None, findings: list[dict] | None = None,
                tool_name: str = "agents-list-appsec", tool_version: str = "0.1.0",
                output_path: str = "deliverables/m2-findings/findings.sarif",
                code_root: str = "repo", resolve_path: Callable | None = None,
                **_: Any) -> dict:
    """Serialise findings to a SARIF 2.1.0 log written under the workspace.

    Each finding needs at least {rule_id, message, file, start_line}; optional
    keys: cvss (a v3.1 vector), severity, start_column, end_line, snippet,
    cwe, owasp, exploit, remediation. `file` is relative to `code_root`
    (default repo); a workspace path under code_root, as scan_secrets reports
    it, is shortened. The score, the severity band and the SARIF level come
    from the vector: a cvss_score or severity that disagrees with it is
    refused. Rules carry `security-severity` (the highest score among their
    results) and the 'security' and CWE tags GitHub code scanning reads.
    SECRET.* results get no snippet, and any secret value the scanner sees at
    that line is redacted from their text. Returns the written path and the
    result/rule counts.
    """
    findings = findings or []
    out = _resolve(workspace, output_path, resolve_path, write=True)
    rules: dict[str, dict] = {}
    rule_scores: dict[str, float] = {}
    results: list[dict] = []
    for idx, f in enumerate(findings):
        if not isinstance(f, dict):
            raise ToolError(f"finding {idx} is not an object")
        rule_id = f.get("rule_id") or f.get("ruleId")
        if not rule_id:
            raise ToolError(f"finding {idx} has no rule_id")
        if "file" not in f or "start_line" not in f:
            raise ToolError(f"finding {idx} ({rule_id}) needs file and start_line")
        try:
            start_line = int(f["start_line"])
        except (TypeError, ValueError):
            raise ToolError(f"finding {idx} ({rule_id}) start_line must be an integer") from None
        sev = str(f.get("severity") or f.get("level") or "").lower()
        score = None
        if f.get("cvss"):
            scored = cvss_base_score(vector=str(f["cvss"]))
            score, band = scored["base_score"], scored["severity"].lower()
            given = f.get("cvss_score")
            if given is not None:
                try:
                    mismatch = abs(float(given) - score) > 1e-9
                except (TypeError, ValueError):
                    mismatch = True
                if mismatch:
                    raise ToolError(f"finding {idx} ({rule_id}): cvss_score {given} does not match its "
                                    f"vector ({score}); leave cvss_score out, the vector decides")
            if sev in _BANDS and sev != band:
                raise ToolError(f"finding {idx} ({rule_id}): severity {sev!r} does not match the "
                                f"vector's band {band!r} ({score})")
            level = sarif_level(score)
        elif f.get("cvss_score") is not None:
            raise ToolError(f"finding {idx} ({rule_id}): cvss_score needs the cvss vector it came from")
        else:
            level = _SARIF_LEVELS.get(sev, "warning")

        texts = {k: str(f[k]) for k in _TEXT_FIELDS if f.get(k)}
        secret = str(rule_id).upper().startswith("SECRET.")
        if secret:
            uri = _artifact_uri(f["file"], code_root)
            for value in _secret_values_near(workspace, code_root, uri, start_line):
                texts = {k: v.replace(value, redact_secret(value)) for k, v in texts.items()}
        props: dict[str, Any] = {}
        for key in ("cwe", "owasp", "cvss"):
            if f.get(key):
                props[key] = f[key]
        for key in ("exploit", "remediation"):
            if texts.get(key):
                props[key] = texts[key]
        if score is not None:
            props["security-severity"] = f"{score:.1f}"
            rule_scores[rule_id] = max(score, rule_scores.get(rule_id, 0.0))
        if rule_id not in rules:
            rule_props: dict[str, Any] = {"tags": ["security", *_cwe_tags(f.get("cwe"))]}
            if f.get("cwe"):
                rule_props["cwe"] = f["cwe"]
            if f.get("owasp"):
                rule_props["owasp"] = f["owasp"]
            name = texts.get("name", rule_id)
            rules[rule_id] = {
                "id": rule_id,
                "name": name,
                "shortDescription": {"text": name},
                "fullDescription": {"text": texts.get("description", texts.get("message", rule_id))},
                "defaultConfiguration": {"level": level},
                "properties": rule_props,
            }
        region: dict[str, Any] = {"startLine": start_line}
        if f.get("start_column"):
            region["startColumn"] = int(f["start_column"])
        if f.get("end_line"):
            region["endLine"] = int(f["end_line"])
        if f.get("snippet") and not secret:
            region["snippet"] = {"text": str(f["snippet"])}
        results.append({
            "ruleId": rule_id,
            "level": level,
            "message": {"text": texts.get("message", texts.get("name", rule_id))},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": _artifact_uri(f["file"], code_root)},
                    "region": region,
                },
            }],
            "properties": props,
        })
    for rule_id, score in rule_scores.items():
        rules[rule_id]["properties"]["security-severity"] = f"{score:.1f}"
        rules[rule_id]["defaultConfiguration"]["level"] = sarif_level(score)

    log = {
        "version": "2.1.0",
        "$schema": _SARIF_SCHEMA,
        "runs": [{
            "tool": {"driver": {
                "name": tool_name,
                "version": tool_version,
                "informationUri": "https://agents.list/specialists/appsec-review",
                "rules": list(rules.values()),
            }},
            "results": results,
        }],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, indent=2, sort_keys=False), encoding="utf-8")
    return {"path": output_path, "results": len(results), "rules": len(rules)}


TOOL_DEFS = [
    {
        "name": "scan_secrets",
        "description": ("Scan every text file under a workspace subtree for hard-coded "
                        "secrets (cloud, GitHub, Stripe and Google keys, private keys, "
                        "tokens, credential assignments, credentials in URLs); returns "
                        "file:line evidence with the secret redacted and lists skipped files."),
        "input_schema": {
            "type": "object",
            "properties": {"root": {"type": "string",
                                    "description": "Workspace-relative root to scan (default repo)."}},
        },
        "risk": "read",
        "function": scan_secrets,
    },
    {
        "name": "audit_dependencies",
        "description": ("Parse a dependency manifest (requirements.txt or npm "
                        "package-lock.json) and query the OSV API for known "
                        "vulnerabilities in each pinned version (only the package name "
                        "and version are sent). Returns advisories with the fixed version "
                        "for the installed branch, plus lines that could not be audited; "
                        "the full audit is written to output_path."),
        "input_schema": {
            "type": "object",
            "properties": {
                "manifest": {"type": "string", "description": "Workspace-relative manifest path."},
                "output_path": {"type": "string",
                                "description": "Where to write the complete audit (default "
                                               "deliverables/m2-findings/dependencies.json)."},
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "Internal package names or prefixes ending in * "
                                           "that must not be sent to OSV."},
            },
        },
        "risk": "network",
        "function": audit_dependencies,
    },
    {
        "name": "parse_openapi",
        "description": ("Enumerate API endpoints from an OpenAPI/Swagger spec (JSON or "
                        "YAML) and summarise the attack surface (auth-required flags, "
                        "parameters, unauthenticated routes, servers)."),
        "input_schema": {
            "type": "object",
            "properties": {"spec_path": {"type": "string",
                                         "description": "Workspace-relative spec path."}},
        },
        "risk": "read",
        "function": parse_openapi,
    },
    {
        "name": "cvss_base_score",
        "description": ("Compute a CVSS v3.1 base score and severity band from a "
                        "vector string such as CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H."),
        "input_schema": {
            "type": "object",
            "properties": {"vector": {"type": "string", "description": "CVSS v3.1 vector."}},
            "required": ["vector"],
        },
        "risk": "read",
        "function": cvss_base_score,
    },
    {
        "name": "check_scope",
        "description": ("Check whether target hosts/URLs fall inside the signed "
                        "scope before any interaction; out-of-scope entries always "
                        "win, and an exact host does not cover its subdomains."),
        "input_schema": {
            "type": "object",
            "properties": {
                "targets": {"type": "array", "items": {"type": "string"},
                            "description": "Hosts or URLs to test against the allow-list."},
                "scope_file": {"type": "string", "description": "Workspace-relative scope file."},
            },
            "required": ["targets"],
        },
        "risk": "read",
        "function": check_scope,
    },
    {
        "name": "build_sarif",
        "description": ("Write a SARIF 2.1.0 report from a findings list; each "
                        "finding carries file:line, a CVSS v3.1 vector (the score, band "
                        "and level are computed from it), CWE/OWASP, exploit scenario and "
                        "remediation. Importable by GitHub code scanning."),
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {"type": "array", "items": {"type": "object"},
                             "description": "Findings with rule_id, file, start_line, cvss, etc."},
                "output_path": {"type": "string", "description": "Workspace-relative SARIF path."},
                "code_root": {"type": "string",
                              "description": "Directory the finding files are relative to (default repo)."},
                "tool_name": {"type": "string"},
                "tool_version": {"type": "string"},
            },
            "required": ["findings"],
        },
        "risk": "write",
        "function": build_sarif,
    },
]
