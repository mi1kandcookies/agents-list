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
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from agentkit.errors import PolicyViolation, ToolError
from agentkit.policy import INTERNAL_DIR, jail_path

# --- workspace helpers -------------------------------------------------------

# Files we never bother scanning: binaries, vendored trees, VCS metadata.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
              "build", ".mypy_cache", ".pytest_cache", "vendor", ".idea"}
_TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".php",
                  ".java", ".cs", ".yaml", ".yml", ".json", ".env", ".ini",
                  ".cfg", ".toml", ".sh", ".txt", ".md", ".xml", ".properties",
                  ".conf", ".tf", ""}


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


def _iter_text_files(root: Path, workspace: Path) -> Iterable[Path]:
    """Yield candidate text files under root, skipping vendored/binary trees,
    kit internals and anything (a symlink) that resolves outside the workspace."""
    workspace = Path(workspace).resolve()
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # Grader only the parts below the scan root, so a workspace that lives
        # under e.g. /srv/build/ is not skipped wholesale.
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            real = path.resolve()
        except OSError:
            continue
        if not real.is_relative_to(workspace):
            continue
        inner = real.relative_to(workspace).parts
        if inner and inner[0] == INTERNAL_DIR:
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2_000_000:  # 2 MB guard
                continue
        except OSError:
            continue
        yield path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _redact(secret: str) -> str:
    """Keep a fingerprint, hide the body: 'AKIA…(20)…' never the full value."""
    secret = secret.strip()
    if len(secret) <= 8:
        return secret[:2] + "***"
    return f"{secret[:4]}***{secret[-2:]} (len {len(secret)})"


# --- secret scanning ---------------------------------------------------------

# In-house patterns, written from first principles (clean-room). Each entry is
# (rule_id, human name, CWE, severity, compiled regex). Patterns are shaped to
# their tokens, not copied from any third-party rule set.
def _secret_patterns() -> list[tuple[str, str, str, str, re.Pattern[str]]]:
    return [
        ("SECRET.AWS_ACCESS_KEY_ID", "AWS access key id", "CWE-798", "high",
         re.compile(r"\b((?:AKIA|ASIA|AIDA|AGPA)[A-Z0-9]{16})\b")),
        ("SECRET.PRIVATE_KEY_BLOCK", "Private key block", "CWE-798", "critical",
         re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
        ("SECRET.GENERIC_ASSIGNMENT", "Hard-coded credential assignment", "CWE-798", "high",
         re.compile(
             r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|access[_-]?key)\b"
             r"\s*[:=]\s*['\"]([^'\"\s]{12,})['\"]")),
        ("SECRET.JWT", "JSON Web Token", "CWE-522", "medium",
         re.compile(r"\bey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
        ("SECRET.SLACK_TOKEN", "Slack token", "CWE-798", "high",
         re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ]


# Assignments whose value is obviously a placeholder are not findings.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{3,}|\.{3,}|<[^>]+>|\{\{?[^}]+\}?\}|your[_-]?|changeme|"
    r"example|placeholder|dummy|sample|todo|redacted|env\.|process\.env|"
    r"os\.environ)")


def scan_secrets(workspace: Path, *, fetch=None, run=None, root: str = "repo",
                 resolve_path: Callable | None = None, **_: Any) -> dict:
    """Scan text files under `root` for hard-coded secrets.

    Returns a findings list with file (workspace-relative), 1-based
    line/column, the rule that hit, a redacted excerpt, CWE and severity, plus
    a count of files scanned. The matched secret is never returned in full.
    """
    base = _resolve(workspace, root, resolve_path)
    ws = Path(workspace).resolve()
    patterns = _secret_patterns()
    findings: list[dict] = []
    scanned = 0
    for path in _iter_text_files(base, ws):
        scanned += 1
        rel = path.relative_to(ws).as_posix()
        text = _read_text(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if len(line) > 4000:  # skip minified blobs
                continue
            for rule_id, name, cwe, severity, pattern in patterns:
                for m in pattern.finditer(line):
                    value = m.group(1) if m.groups() else m.group(0)
                    if _PLACEHOLDER.match(value.strip()):
                        continue
                    findings.append({
                        "rule_id": rule_id,
                        "name": name,
                        "file": rel,
                        "line": lineno,
                        "column": m.start() + 1,
                        "excerpt": _redact(value),
                        "cwe": cwe,
                        "severity": severity,
                    })
    findings.sort(key=lambda f: (f["file"], f["line"], f["column"], f["rule_id"]))
    return {"findings": findings, "files_scanned": scanned,
            "secret_count": len(findings)}


# --- dependency audit (OSV) --------------------------------------------------

def _parse_requirements(text: str) -> list[dict]:
    """Very small requirements.txt reader: name==version lines only."""
    pkgs: list[dict] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-+]+)", line)
        if m:
            pkgs.append({"name": m.group(1), "version": m.group(2),
                         "ecosystem": "PyPI"})
    return pkgs


def _parse_package_lock(text: str) -> list[dict]:
    """npm package-lock.json (v2/v3 `packages` map) -> pinned dependencies."""
    data = json.loads(text)
    pkgs: list[dict] = []
    for key, meta in (data.get("packages") or {}).items():
        if not key or "node_modules/" not in f"{key}/":
            continue
        name = key.split("node_modules/")[-1]
        version = (meta or {}).get("version")
        if name and version:
            pkgs.append({"name": name, "version": version, "ecosystem": "npm"})
    # Fallback for lockfile v1 `dependencies`.
    for name, meta in (data.get("dependencies") or {}).items():
        version = (meta or {}).get("version")
        if name and version:
            pkgs.append({"name": name, "version": version, "ecosystem": "npm"})
    seen, out = set(), []
    for p in pkgs:
        key = (p["name"], p["version"])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _osv_severity(vuln: dict) -> str:
    """Pull a coarse severity label from an OSV record's ecosystem data."""
    for sev in vuln.get("severity", []) or []:
        score = str(sev.get("score", ""))
        if score:
            return score
    db = (vuln.get("database_specific") or {}).get("severity")
    return str(db) if db else "UNKNOWN"


def _osv_fixed(vuln: dict, name: str) -> str:
    """First fixed version OSV reports for `name`, or '' if none stated."""
    for aff in vuln.get("affected", []) or []:
        if (aff.get("package") or {}).get("name") != name:
            continue
        for rng in aff.get("ranges", []) or []:
            for ev in rng.get("events", []) or []:
                if "fixed" in ev:
                    return ev["fixed"]
    return ""


def audit_dependencies(workspace: Path, *, fetch: Callable | None = None, run=None,
                       manifest: str = "repo/requirements.txt",
                       resolve_path: Callable | None = None, ledger: Any = None,
                       **_: Any) -> dict:
    """Parse a dependency manifest and query OSV for known vulnerabilities.

    Uses the injected `fetch` to POST each pinned package to the OSV query API;
    raises ToolError if no fetcher is available (network is policy-gated). The
    manifest may be a requirements.txt or an npm package-lock.json. With a
    `ledger`, each OSV answer that lists a vulnerability is registered as a
    source (kind "tool") and its id returned as the vulnerability's `source`.
    """
    path = _resolve(workspace, manifest, resolve_path)
    if not path.exists():
        raise ToolError(f"manifest not found: {manifest}")
    text = _read_text(path)
    if path.name == "package-lock.json" or manifest.endswith(".json"):
        packages = _parse_package_lock(text)
    else:
        packages = _parse_requirements(text)
    if fetch is None:
        raise ToolError("audit_dependencies needs network access (fetch); "
                        "OSV egress must be on the allow-list")

    vulnerabilities: list[dict] = []
    for pkg in packages:
        body = json.dumps({
            "version": pkg["version"],
            "package": {"name": pkg["name"], "ecosystem": pkg["ecosystem"]},
        })
        resp = fetch("https://api.osv.dev/v1/query", method="POST",
                     headers={"Content-Type": "application/json"}, body=body)
        status = getattr(resp, "status", 200)
        if status != 200:
            raise ToolError(f"OSV query failed for {pkg['name']}: HTTP {status}")
        text = getattr(resp, "text", "") or "{}"
        vulns = json.loads(text).get("vulns", []) or []
        source_id = ""
        if vulns and ledger is not None:
            source_id = ledger.add_source(
                f"osv:{pkg['ecosystem']}/{pkg['name']}@{pkg['version']}",
                f"OSV advisories for {pkg['name']} {pkg['version']} ({pkg['ecosystem']})",
                text, kind="tool").id
        for vuln in vulns:
            vulnerabilities.append({
                "package": pkg["name"],
                "version": pkg["version"],
                "ecosystem": pkg["ecosystem"],
                "id": vuln.get("id", ""),
                "summary": vuln.get("summary", "") or vuln.get("details", "")[:200],
                "severity": _osv_severity(vuln),
                "fixed": _osv_fixed(vuln, pkg["name"]),
                "aliases": vuln.get("aliases", []) or [],
                "source": source_id,
            })
    vulnerabilities.sort(key=lambda v: (v["package"], v["id"]))
    return {"packages": packages, "package_count": len(packages),
            "vulnerabilities": vulnerabilities,
            "vulnerable_count": len({v["package"] for v in vulnerabilities})}


# --- OpenAPI / attack surface ------------------------------------------------

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def parse_openapi(workspace: Path, *, fetch=None, run=None,
                  spec_path: str = "inputs/openapi.json",
                  resolve_path: Callable | None = None, **_: Any) -> dict:
    """Enumerate endpoints from an OpenAPI/Swagger JSON spec.

    Returns every operation with its method, path, operationId, whether auth is
    required (from `security`), and its parameter names, plus an attack-surface
    summary: total endpoints, unauthenticated endpoints and declared servers.
    """
    path = _resolve(workspace, spec_path, resolve_path)
    if not path.exists():
        raise ToolError(f"spec not found: {spec_path}")
    spec = json.loads(_read_text(path))
    global_security = spec.get("security")
    servers = [s.get("url", "") for s in spec.get("servers", []) or []]
    if not servers and spec.get("host"):  # swagger 2.0
        scheme = (spec.get("schemes") or ["https"])[0]
        servers = [f"{scheme}://{spec['host']}{spec.get('basePath', '')}"]

    endpoints: list[dict] = []
    for route, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters", []) or []
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            op_security = op.get("security", global_security)
            auth_required = bool(op_security)
            params = shared_params + (op.get("parameters", []) or [])
            param_names = sorted({p.get("name", "") for p in params if isinstance(p, dict) and p.get("name")})
            has_body = "requestBody" in op or any(
                isinstance(p, dict) and p.get("in") == "body" for p in params)
            endpoints.append({
                "method": method.upper(),
                "path": route,
                "operation_id": op.get("operationId", ""),
                "auth_required": auth_required,
                "parameters": param_names,
                "accepts_body": has_body,
            })
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    unauth = [f"{e['method']} {e['path']}" for e in endpoints if not e["auth_required"]]
    return {"endpoints": endpoints, "count": len(endpoints),
            "unauthenticated": unauth, "servers": servers}


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


def cvss_base_score(workspace: Path | None = None, *, fetch=None, run=None,
                    vector: str = "", **_: Any) -> dict:
    """Compute a CVSS v3.1 base score and severity from a vector string.

    Example vector: 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'. Raises
    ToolError on a missing or malformed vector so a finding can never carry a
    silently-wrong score.
    """
    if not vector:
        raise ToolError("cvss_base_score needs a vector string")
    parts = {}
    for token in vector.strip().split("/"):
        if ":" not in token:
            continue
        key, val = token.split(":", 1)
        parts[key.strip().upper()] = val.strip().upper()
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
        raise ToolError(f"CVSS vector missing/invalid metric {exc}: {vector}")

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
    return {"vector": vector, "base_score": score,
            "severity": _cvss_severity(score)}


# --- scope allow-list guard --------------------------------------------------

def _host_of(target: str) -> str:
    """Bare host from a URL or host string, port and path stripped."""
    t = target.strip()
    t = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", t)  # drop scheme
    t = t.split("/", 1)[0].split("@")[-1]               # drop path, userinfo
    return t.split(":", 1)[0].lower()                   # drop port


def _host_in_scope(host: str, allow: list[str]) -> bool:
    """True if host equals or is a subdomain of any allow entry (or *.dom)."""
    host = host.lower()
    for entry in allow:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("*."):
            base = entry[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def check_scope(workspace: Path, *, fetch=None, run=None, targets: list[str] | None = None,
                scope_file: str = "inputs/scope.json",
                resolve_path: Callable | None = None, **_: Any) -> dict:
    """Decide whether each target is inside the signed scope allow-list.

    The allow-list is `{"hosts": [...]}` (entries may be exact hosts or `*.dom`
    wildcards) read from `scope_file`. Returns a per-target verdict plus an
    `all_in_scope` flag so the agent can refuse out-of-scope work.
    """
    path = _resolve(workspace, scope_file, resolve_path)
    if not path.exists():
        raise ToolError(f"scope file not found: {scope_file}")
    allow = list((json.loads(_read_text(path)) or {}).get("hosts", []))
    results = []
    for target in (targets or []):
        host = _host_of(target)
        ok = _host_in_scope(host, allow)
        results.append({"target": target, "host": host, "in_scope": ok,
                        "reason": "matches allow-list" if ok else "not in allow-list"})
    return {"results": results, "allow": allow,
            "all_in_scope": all(r["in_scope"] for r in results) if results else True}


# --- SARIF 2.1.0 writer ------------------------------------------------------

_SARIF_LEVELS = {"critical": "error", "high": "error", "medium": "warning",
                 "low": "note", "none": "none", "error": "error",
                 "warning": "warning", "note": "note"}
_SARIF_SCHEMA = ("https://raw.githubusercontent.com/oasis-tcs/sarif-spec/"
                 "master/Schemata/sarif-schema-2.1.0.json")


def _artifact_uri(file: Any, code_root: str) -> str:
    """SARIF artifact URI relative to the code root (what code scanning and
    sarif_locations_exist expect): a workspace path such as repo/app.py, as
    scan_secrets reports it, becomes app.py."""
    uri = str(file).replace("\\", "/")
    root = (code_root or "").replace("\\", "/").strip("/")
    if root and root != "." and uri.startswith(root + "/"):
        uri = uri[len(root) + 1:]
    return uri


def build_sarif(workspace: Path, *, fetch=None, run=None, findings: list[dict] | None = None,
                tool_name: str = "agents-list-appsec", tool_version: str = "0.1.0",
                output_path: str = "deliverables/m2-findings/findings.sarif",
                code_root: str = "repo", resolve_path: Callable | None = None,
                **_: Any) -> dict:
    """Serialise findings to a SARIF 2.1.0 log written under the workspace.

    Each finding needs at least {rule_id, message, file, start_line}; optional
    keys: level|severity, start_column, end_line, snippet, cwe, owasp, cvss,
    exploit, remediation. `file` is relative to `code_root` (default repo); a
    workspace path under code_root, as scan_secrets reports it, is shortened.
    Severity maps to a SARIF level and to a numeric `security-severity`
    property so GitHub code scanning can rank it. Returns the written path and
    the result/rule counts.
    """
    findings = findings or []
    rules: dict[str, dict] = {}
    results: list[dict] = []
    for idx, f in enumerate(findings):
        rule_id = f.get("rule_id") or f.get("ruleId")
        if not rule_id:
            raise ToolError(f"finding {idx} has no rule_id")
        if "file" not in f or "start_line" not in f:
            raise ToolError(f"finding {idx} ({rule_id}) needs file and start_line")
        sev = str(f.get("severity", f.get("level", "warning"))).lower()
        level = _SARIF_LEVELS.get(sev, "warning")
        props = {}
        for key in ("cwe", "owasp", "cvss", "exploit", "remediation"):
            if f.get(key):
                props[key] = f[key]
        if f.get("cvss_score") is not None:
            props["security-severity"] = str(f["cvss_score"])
        if rule_id not in rules:
            rule_props = {}
            if f.get("cwe"):
                rule_props["cwe"] = f["cwe"]
            if f.get("owasp"):
                rule_props["owasp"] = f["owasp"]
            rules[rule_id] = {
                "id": rule_id,
                "name": f.get("name", rule_id),
                "shortDescription": {"text": f.get("name", rule_id)},
                "fullDescription": {"text": f.get("description", f.get("message", rule_id))},
                "defaultConfiguration": {"level": level},
                "properties": rule_props,
            }
        region: dict[str, Any] = {"startLine": int(f["start_line"])}
        if f.get("start_column"):
            region["startColumn"] = int(f["start_column"])
        if f.get("end_line"):
            region["endLine"] = int(f["end_line"])
        if f.get("snippet"):
            region["snippet"] = {"text": f["snippet"]}
        results.append({
            "ruleId": rule_id,
            "level": level,
            "message": {"text": f.get("message", f.get("name", rule_id))},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": _artifact_uri(f["file"], code_root)},
                    "region": region,
                },
            }],
            "properties": props,
        })

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
    out = _resolve(workspace, output_path, resolve_path, write=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, indent=2, sort_keys=False), encoding="utf-8")
    return {"path": output_path, "results": len(results), "rules": len(rules)}


TOOL_DEFS = [
    {
        "name": "scan_secrets",
        "description": ("Scan source files under a workspace subtree for hard-coded "
                        "secrets (API keys, private keys, tokens); returns file:line "
                        "evidence with the secret redacted."),
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
                        "vulnerabilities in each pinned version."),
        "input_schema": {
            "type": "object",
            "properties": {"manifest": {"type": "string",
                                        "description": "Workspace-relative manifest path."}},
        },
        "risk": "network",
        "function": audit_dependencies,
    },
    {
        "name": "parse_openapi",
        "description": ("Enumerate API endpoints from an OpenAPI/Swagger JSON spec "
                        "and summarise the attack surface (auth-required flags, "
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
                        "scope allow-list before any interaction; refuses "
                        "out-of-scope work."),
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
                        "finding carries file:line, level, CWE/OWASP, CVSS, exploit "
                        "scenario and remediation. Importable by GitHub code scanning."),
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {"type": "array", "items": {"type": "object"},
                             "description": "Findings with rule_id, file, start_line, etc."},
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
