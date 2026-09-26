"""
agentkit/policy.py - the PolicyGate: what a specialist's tools may do.

Every tool call passes through the gate before it runs, and the ToolContext
helpers (fetch, run, path resolution) consult it again. Domain tools get the
same helpers (fetch, run, resolve_path); a domain tool that opens files
through its raw `workspace` Path instead is outside the gate, which is why
the kit hands it resolve_path:

    tools      only the tools listed in agent.yaml; risk "external" (sending,
               filing, signing, paying) is denied in v1
    shell      executables allowlisted by basename (".exe" stripped), argv
               lists only (never a shell string); ToolContext.run looks a
               bare name up on the scrubbed PATH and never runs a program
               from inside the workspace
    egress     mode "none" denies all network; "allowlist" matches hosts with
               security.host_allowed, optionally narrowed to a second list
               (egress_narrow: a host must match both) or extended with
               exact hostnames (extra_egress_allow, never wildcards) - both
               fed from the brief only when the manifest opts in
               (egress.intake_field); private and metadata hosts always denied,
               including numeric spellings (2130706433, 127.1) and IPv6 forms
               that embed a private IPv4 address. A hostname is judged by what
               it resolves to: the default fetch transport checks every DNS
               answer and connects only to the address it checked
    workspace  paths are checked lexically first (no drive, root, UNC or
               device prefix, no ".." escape, no ":" on Windows), so a
               hostile path never touches the filesystem or the network;
               then they resolve inside the workspace (symlinks included);
               inputs/ is read-only; .agentkit/ is internal to the kit

Violations raise PolicyViolation; the loop reports them to the model as tool
errors and records a policy event.

What the gate cannot contain: an allowlisted program runs whatever code it
is handed (python -c, a conftest.py, npm scripts, git aliases and hooks).
Such a process can write anywhere the VM user can (inputs/, .agentkit/),
open sockets to any host and read outside the workspace. The rules above
bind the kit's own tools; a specialist whose manifest allowlists any program
must also run inside an OS sandbox that enforces the same boundaries
(see docs/decisions/0002-specialist-kit.md, "Containment").
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Iterable, Sequence
from urllib.parse import urlsplit

from agentkit.errors import PolicyViolation
from agentkit.security import host_allowed, is_numeric_host, is_private_host, normalize_host

if TYPE_CHECKING:
    from agentkit.manifest import Manifest

READ_ONLY_DIRS = ("inputs",)
INTERNAL_DIR = ".agentkit"
DENIED_RISKS = ("external",)
_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                       r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_LABELS = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


def _host_list(values: Iterable[str], *, rules: bool, what: str) -> list[str]:
    """Validate hostnames (rules=False: exact names, no IP literals) or host
    rules (rules=True: also ".example.com", "*.example.com" and "*")."""
    out = []
    for v in values:
        host = normalize_host(v) if isinstance(v, str) else ""
        if rules and host.startswith(("*.", ".")):
            ok = bool(_LABELS.match(host.lstrip("*").lstrip(".")))   # a suffix rule, e.g. ".gov"
        else:
            ok = (rules and host == "*") or bool(_HOSTNAME.match(host))
        ok = ok and not is_numeric_host(host)
        if not ok:
            raise PolicyViolation(f"{what}: {v!r} is not {'a host rule' if rules else 'a hostname'}")
        out.append(host)
    return out


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


def jail_path(workspace: Path, path: str) -> Path:
    """Resolve a path that must stay inside `workspace`.

    The input is checked lexically before anything touches the filesystem:
    on Windows, resolving "//host/share/x" would already open an SMB
    connection (DNS lookup, NTLM handshake) to a host the caller picked.
    Only after the lexical check is the path resolved, which catches
    symlink and junction escapes. Raises PolicyViolation.
    """
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise PolicyViolation("path must be a non-empty string")
    if path.startswith(("\\\\", "//")):
        raise PolicyViolation(f"path {path!r} is a UNC or device path")
    if os.name == "nt":
        win = PureWindowsPath(path)
        rest = path[len(win.drive):]
        if (win.drive and (len(win.drive) != 2 or not win.root)) or ":" in rest:
            raise PolicyViolation(f"path {path!r} has a drive-relative, stream or device part")
    root = Path(workspace).resolve()
    roots = (root, Path(os.path.normpath(os.path.abspath(workspace))))
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.normpath(candidate))
    if not _inside(lexical, roots):
        raise PolicyViolation(f"path {path!r} is outside the workspace")
    resolved = lexical.resolve()
    if not _inside(resolved, (root,)):
        raise PolicyViolation(f"path {path!r} is outside the workspace")
    return resolved


def executable_name(arg0: str) -> str:
    """Basename of argv[0], lowercased, with a trailing .exe removed."""
    name = PurePosixPath(str(arg0).replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


class PolicyGate:
    def __init__(self, manifest: "Manifest | None" = None, *,
                 tools: Iterable[str] | None = None,
                 shell_allow: Iterable[str] | None = None,
                 shell_timeout: float | None = None,
                 egress_mode: str | None = None,
                 egress_allow: Iterable[str] | None = None,
                 extra_egress_allow: Iterable[str] = (),
                 egress_narrow: Iterable[str] | None = None):
        """Values come from the manifest; keyword arguments override them.

        tools=None without a manifest means "any registered tool" (tests).
        extra_egress_allow adds exact hostnames (no "*" or wildcard rules)
        but can never turn egress on when the manifest mode is "none".
        egress_narrow, when given, is a second list of host rules every URL
        must also match. Malformed entries raise PolicyViolation.
        """
        m = manifest
        self.tools: set[str] | None = (set(tools) if tools is not None
                                       else set(m.tools) if m is not None else None)
        allow = shell_allow if shell_allow is not None else (m.shell.allow if m else [])
        self.shell_allow = {executable_name(a) for a in allow}
        self.shell_timeout = float(shell_timeout if shell_timeout is not None
                                   else (m.shell.timeout_seconds if m else 600))
        self.egress_mode = egress_mode or (m.egress.mode if m else "none")
        self.egress_allow = list(egress_allow if egress_allow is not None
                                 else (m.egress.allow if m else []))
        extra = _host_list(extra_egress_allow, rules=False, what="extra egress host")
        if self.egress_mode == "allowlist":
            self.egress_allow += extra
        self.egress_narrow = (None if egress_narrow is None
                              else _host_list(egress_narrow, rules=True, what="allowed domain"))

    # --- tools --------------------------------------------------------------

    def check_tool(self, name: str, risk: str = "read") -> None:
        if risk in DENIED_RISKS:
            raise PolicyViolation(f"tool {name!r} has risk {risk!r}, which is not allowed")
        if self.tools is not None and name not in self.tools:
            raise PolicyViolation(f"tool {name!r} is not enabled for this specialist")

    # --- shell --------------------------------------------------------------

    def check_command(self, argv: Sequence[str]) -> str:
        """Validate an argv list; return the normalized executable name."""
        if isinstance(argv, (str, bytes)) or not argv:
            raise PolicyViolation("commands must be a non-empty argv list, not a shell string")
        if not all(isinstance(a, str) for a in argv):
            raise PolicyViolation("every argv element must be a string")
        exe = executable_name(argv[0])
        if exe not in self.shell_allow:
            allowed = ", ".join(sorted(self.shell_allow)) or "none"
            raise PolicyViolation(f"command {exe!r} is not allowlisted (allowed: {allowed})")
        return exe

    # --- egress -------------------------------------------------------------

    def check_url(self, url: str) -> str:
        """Validate an outbound URL; return its normalized host."""
        parts = urlsplit(url or "")
        if parts.scheme not in ("http", "https"):
            raise PolicyViolation(f"only http(s) URLs are allowed, got {parts.scheme or 'none'!r}")
        if parts.username or parts.password:
            raise PolicyViolation("URLs with embedded credentials are not allowed")
        host = normalize_host(parts.hostname or "")
        if not host:
            raise PolicyViolation("URL has no host")
        if self.egress_mode != "allowlist":
            raise PolicyViolation("network access is disabled for this specialist")
        if is_private_host(host):
            raise PolicyViolation(f"host {host!r} is private or internal")
        if not host_allowed(host, self.egress_allow):
            raise PolicyViolation(f"host {host!r} is not on the egress allowlist")
        if self.egress_narrow is not None and not host_allowed(host, self.egress_narrow):
            raise PolicyViolation(f"host {host!r} is not among this engagement's allowed domains")
        return host

    # --- workspace ------------------------------------------------------------

    def resolve_path(self, workspace: Path, path: str, *, write: bool = False) -> Path:
        """Resolve a model-supplied path inside the workspace.

        Rejects escapes (.., absolute paths elsewhere, symlinks pointing out),
        any access to .agentkit/, and writes under inputs/.
        """
        root = Path(workspace).resolve()
        resolved = jail_path(root, path)
        rel = resolved.relative_to(root)
        first = rel.parts[0].lower() if rel.parts else ""
        if first == INTERNAL_DIR:
            raise PolicyViolation(f"path {path!r} is internal to the kit")
        if write and (first in READ_ONLY_DIRS or not rel.parts):
            raise PolicyViolation(f"path {path!r} is read-only")
        return resolved

    @staticmethod
    def relative(workspace: Path, resolved: Path) -> str:
        """Workspace-relative, forward-slash form of a resolved path."""
        return resolved.relative_to(Path(workspace).resolve()).as_posix()


__all__ = ["DENIED_RISKS", "INTERNAL_DIR", "PolicyGate", "READ_ONLY_DIRS", "executable_name",
           "jail_path"]
