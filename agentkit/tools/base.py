"""
agentkit/tools/base.py - Tool, ToolContext and ToolRegistry.

A Tool is a name, a description, a JSON Schema for its input, a risk level
and a handler(args, ctx). The ToolContext is everything a handler may touch,
and every side door goes through the PolicyGate:

    ctx.path(p, write=False)   workspace-jailed path
    ctx.fetch(url, ...)        egress-checked HTTP, redirects re-checked
                               (credentials and body dropped on a
                               cross-origin hop), size-capped, overall
                               deadline -> FetchResult. The default
                               transport resolves the host itself, refuses
                               any non-public address and connects to the
                               address it checked (no DNS rebinding).
    ctx.run(argv, ...)         shell-policy-checked subprocess, shell=False,
                               scrubbed env, truncated output -> CommandResult.
                               The timeout bounds the whole call: the child
                               runs in its own process group (POSIX) or job
                               object (Windows), and the whole tree is killed
                               on timeout and again when the child exits, so
                               nothing it started outlives the step. Output
                               is captured incrementally with a byte cap.

The allowlist names programs, not behaviors: an allowlisted interpreter or
build tool (python, node, pytest, npm, git) runs whatever code it is given.
The PolicyGate cannot contain that; an exec-capable specialist must run in
an OS sandbox (container or VM with inputs/ and .agentkit/ outside the
agent's write access, and network egress enforced by the host). See
docs/decisions/0002-specialist-kit.md.

Specialists write domain tools as plain functions
`fn(workspace, *, fetch=None, run=None, **args) -> dict | str` and list them
in TOOL_DEFS; tools_from_defs() wraps them. A function also receives, when
its signature names them:

    resolve_path(p, *, write=False) -> Path   ctx.path: workspace jail,
                                              inputs/ read-only, .agentkit/
                                              refused; write=True also marks
                                              the file as agent-authored so
                                              it can never be a ledger source
    ledger                                    the claim Ledger, e.g.
                                              ledger.add_source(url, title,
                                              text) for fetched data
    events                                    the run's EventSink

`workspace` itself is a plain Path: code that opens files through it
bypasses the jail, so domain tools should resolve model-supplied paths
with resolve_path. Domain tool output is wrapped as untrusted unless the
def sets "untrusted_output": False, and "risk" must be one of
read | write | exec | network | external.
"""
from __future__ import annotations

import functools
import http.client
import inspect
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

from agentkit.errors import AgentKitError, BudgetExceeded, PolicyViolation, ToolError
from agentkit.events import EventSink, NullSink
from agentkit.ledger import Ledger
from agentkit.policy import PolicyGate
from agentkit.security import is_public_ip, redact, secrets_from_env, wrap_untrusted
from agentkit.types import Brief, MilestoneSpec, ToolCall, ToolResult, ToolRisk, ToolSpec

MAX_OUTPUT_CHARS = 30_000
MAX_CAPTURE_BYTES = 2_000_000   # per stream while a command runs (head + tail kept)
MAX_FETCH_BYTES = 5_000_000
MAX_REDIRECTS = 5
FETCH_TIMEOUT = 30.0      # per socket operation
FETCH_DEADLINE = 120.0    # whole response, so a drip-feeding server cannot hold a step
# Environment variables a subprocess may inherit; everything else (API keys,
# tokens, wallet keys) is dropped.
SAFE_ENV_KEYS = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP",
                 "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TZ")

Transport = Callable[[str, str, dict, "bytes | None", float], tuple[int, dict, bytes]]
Handler = Callable[[dict, "ToolContext"], Any]
TOOL_RISKS: tuple[str, ...] = ToolRisk.__args__  # type: ignore[attr-defined]


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    text: str
    truncated: bool = False


@dataclass
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"argv": self.argv, "exit_code": self.exit_code, "stdout": self.stdout,
                "stderr": self.stderr, "timed_out": self.timed_out}


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and tail of long output with a marker in between."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} characters truncated ...]\n{text[-tail:]}"


def scrubbed_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    src = os.environ if env is None else env
    return {k: src[k] for k in SAFE_ENV_KEYS if k in src}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to an already-checked address; the Host header keeps the name."""

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout,
                                             self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to an already-checked address; SNI and the certificate check use the name."""

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout,
                                        self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: str):
        super().__init__()
        self.pinned_ip = pinned_ip

    def http_open(self, req: Any) -> Any:
        return self.do_open(functools.partial(_PinnedHTTPConnection, pinned_ip=self.pinned_ip), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str):
        super().__init__()
        self.pinned_ip = pinned_ip

    def https_open(self, req: Any) -> Any:
        return self.do_open(functools.partial(_PinnedHTTPSConnection, pinned_ip=self.pinned_ip),
                            req, context=self._context)


def resolve_public(host: str, port: int) -> list[str]:
    """Resolve `host` and return its addresses; PolicyViolation if ANY answer
    is not a public address (loopback, private, link-local/metadata, ...)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (UnicodeError, OSError) as exc:
        raise ToolError(f"fetch failed: cannot resolve {host}: {exc}") from None
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise ToolError(f"fetch failed: {host} has no addresses")
    for address in addresses:
        if not is_public_ip(address):
            raise PolicyViolation(f"host {host!r} resolves to a non-public address ({address})")
    return addresses


def read_capped(resp: Any, limit: int, deadline: float,
                clock: Callable[[], float] = time.monotonic) -> bytes:
    """Read at most limit + 1 bytes (the extra byte marks truncation); give up
    with a ToolError once `deadline` (a clock() value) has passed."""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        if clock() > deadline:
            raise ToolError(f"fetch failed: response took longer than {FETCH_DEADLINE:.0f} seconds")
        chunk = resp.read(min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None,
                     timeout: float) -> tuple[int, dict, bytes]:
    """Default transport: urllib without automatic redirects (ctx.fetch follows
    them itself so every hop is policy-checked) and without environment
    proxies, connecting only to an address resolve_public() accepted."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    pinned = resolve_public(parts.hostname or "", port)[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect,
                                         _PinnedHTTPHandler(pinned), _PinnedHTTPSHandler(pinned))
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    deadline = time.monotonic() + FETCH_DEADLINE
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), read_capped(resp, MAX_FETCH_BYTES, deadline)
    except urllib.error.HTTPError as exc:
        return (exc.code, dict(exc.headers.items()) if exc.headers else {},
                read_capped(exc, MAX_FETCH_BYTES, deadline))
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise ToolError(f"fetch failed: {exc}") from None


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port


class _Capture:
    """Keeps the first and last MAX_CAPTURE_BYTES / 2 bytes of a stream."""

    def __init__(self, limit: int):
        self.half = max(limit // 2, 1)
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> None:
        room = self.half - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            extra = len(self.tail) - self.half
            if extra > 0:
                self.dropped += extra
                del self.tail[:extra]

    def value(self) -> bytes:
        if not self.dropped:
            return bytes(self.head + self.tail)
        marker = f"\n[... {self.dropped} bytes dropped ...]\n".encode()
        return bytes(self.head) + marker + bytes(self.tail)


def _pump(stream: Any, capture: _Capture) -> None:
    try:
        for chunk in iter(lambda: stream.read1(65536), b""):
            capture.feed(chunk)
    except (OSError, ValueError):
        pass


class _WindowsJob:
    """A job object that kills every process in it when terminated/closed."""

    def __init__(self, proc: subprocess.Popen):
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("CreateJobObjectW failed")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not k32.AssignProcessToJobObject(self.handle, int(proc._handle)):  # type: ignore[attr-defined]
            k32.CloseHandle(self.handle)
            raise OSError("AssignProcessToJobObject failed")

    def kill(self) -> None:
        if self.handle:
            self.k32.TerminateJobObject(self.handle, 1)
            self.k32.CloseHandle(self.handle)
            self.handle = None


def _kill_tree(proc: subprocess.Popen, job: "_WindowsJob | None") -> None:
    """Kill the child and everything it started (best effort beyond the
    process group / job: a descendant that calls setsid() escapes it)."""
    if job is not None:
        job.kill()
    elif os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc.poll() is None:
        proc.kill()


def subprocess_runner(argv: list[str], cwd: Path, timeout: float,
                      env: dict[str, str]) -> tuple[int, bytes, bytes, bool]:
    """Run argv with an overall deadline; returns (exit_code, stdout, stderr,
    timed_out). exit_code is -1 on timeout."""
    kwargs: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    except FileNotFoundError:
        raise ToolError(f"executable not found: {argv[0]}") from None
    job = None
    if os.name == "nt":
        try:
            job = _WindowsJob(proc)
        except (OSError, AttributeError, ImportError):
            job = None  # fall back to killing the direct child only
    out, err = _Capture(MAX_CAPTURE_BYTES), _Capture(MAX_CAPTURE_BYTES)
    readers = [threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True)]
    for t in readers:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        _kill_tree(proc, job)  # also reaps background children of a finished command
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for t in readers:
            t.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
    return code, out.value(), err.value(), timed_out


@dataclass
class ToolContext:
    workspace: Path
    policy: PolicyGate
    brief: Brief | None = None
    milestone: MilestoneSpec | None = None
    events: EventSink = field(default_factory=NullSink)
    ledger: Ledger | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Pluggable services (e.g. "search": fn(query, limit) -> list[dict]).
    services: dict[str, Any] = field(default_factory=dict)
    transport: Transport = urllib_transport
    runner: Callable[..., tuple[int, bytes, bytes, bool]] = subprocess_runner
    max_output_chars: int = MAX_OUTPUT_CHARS
    max_fetch_bytes: int = MAX_FETCH_BYTES
    # Run-scoped state shared with the loop: "submitted", "questions".
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        if self.ledger is None:
            self.ledger = Ledger(self.workspace)

    @property
    def secrets(self) -> list[str]:
        return secrets_from_env(self.env)

    def path(self, path: str, *, write: bool = False) -> Path:
        return self.policy.resolve_path(self.workspace, path, write=write)

    def rel(self, resolved: Path) -> str:
        return self.policy.relative(self.workspace, resolved)

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              body: bytes | str | None = None) -> FetchResult:
        method = method.upper()
        if method not in ("GET", "HEAD", "POST"):
            raise PolicyViolation(f"HTTP method {method} is not allowed")
        data = body.encode("utf-8") if isinstance(body, str) else body
        hdrs = {"User-Agent": "agentkit/1"} | dict(headers or {})
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self.policy.check_url(current)
            status, resp_headers, raw = self.transport(method, current, hdrs, data, FETCH_TIMEOUT)
            lower = {k.lower(): v for k, v in resp_headers.items()}
            if status in (301, 302, 303, 307, 308) and lower.get("location"):
                target = urljoin(current, lower["location"])
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, data = "GET", None
                if _origin(target) != _origin(current):
                    # Never hand one host's credentials or request body to another.
                    hdrs = {k: v for k, v in hdrs.items() if k.lower() == "user-agent"}
                    if data is not None:
                        method, data = "GET", None
                current = target
                continue
            truncated = len(raw) > self.max_fetch_bytes
            raw = raw[: self.max_fetch_bytes]
            charset = "utf-8"
            ctype = lower.get("content-type", "")
            if "charset=" in ctype:
                charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            self.events.emit("egress", url=current, method=method, status=status, bytes=len(raw))
            return FetchResult(url=current, status=status, headers=resp_headers, body=raw,
                               text=text, truncated=truncated)
        raise ToolError(f"too many redirects fetching {url}")

    def executable(self, arg0: str, env: Mapping[str, str]) -> str:
        """Where argv[0] runs from: a bare name is looked up on the scrubbed
        PATH (shutil.which; on Windows it also tries the harness's current
        directory first); whatever it resolves to must not be inside the
        workspace, where the agent can plant files; Windows batch files are
        refused (cmd.exe re-parses their arguments)."""
        if "/" in arg0 or "\\" in arg0:
            if arg0.startswith(("\\\\", "//")):
                raise PolicyViolation(f"program {arg0!r} is a UNC or device path")
            candidate = Path(arg0)
            if not candidate.is_absolute():
                raise PolicyViolation(f"program {arg0!r} is inside the workspace; "
                                      "only installed programs can be run")
            found = str(candidate)
        else:
            found = shutil.which(arg0, path=env.get("PATH", "")) or ""
            if not found:
                raise ToolError(f"executable not found on PATH: {arg0}")
        real = Path(os.path.normpath(os.path.abspath(found)))
        try:
            real = real.resolve()
        except OSError:
            pass
        if real == self.workspace or real.is_relative_to(self.workspace):
            raise PolicyViolation(f"program {arg0!r} is inside the workspace; "
                                  "only installed programs can be run")
        if real.suffix.lower() in (".bat", ".cmd"):
            raise PolicyViolation(f"program {arg0!r} is a batch file, which is not run")
        return found

    def run(self, argv: Sequence[str], *, cwd: str | None = None,
            timeout: float | None = None) -> CommandResult:
        self.policy.check_command(argv)
        workdir = self.path(cwd) if cwd else self.workspace
        limit = self.policy.shell_timeout if timeout is None else min(float(timeout), self.policy.shell_timeout)
        env = scrubbed_env()
        program = self.executable(argv[0], env)
        code, out, err, timed_out = self.runner([program, *argv[1:]], workdir, limit, env)
        secrets = self.secrets
        result = CommandResult(
            argv=list(argv), exit_code=code, timed_out=timed_out,
            stdout=truncate(redact(out.decode("utf-8", errors="replace"), secrets), self.max_output_chars),
            stderr=truncate(redact(err.decode("utf-8", errors="replace"), secrets), self.max_output_chars),
        )
        self.events.emit("command", argv=list(argv), exit_code=code, timed_out=timed_out)
        return result


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    risk: str = "read"              # read | write | exec | network | external
    untrusted_output: bool = False  # wrap output as untrusted data for the model

    def __post_init__(self) -> None:
        if self.risk not in TOOL_RISKS:
            raise ValueError(f"tool {self.name!r}: risk must be one of {', '.join(TOOL_RISKS)}, "
                             f"got {self.risk!r}")

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, input_schema=self.input_schema)


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "array": list, "object": dict}


def validate_args(schema: Mapping[str, Any], args: Any) -> str | None:
    """Shallow JSON Schema check: object, required keys, top-level types,
    enums and additionalProperties=false. Returns a problem or None."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    props = schema.get("properties", {}) or {}
    for key in schema.get("required", []) or []:
        if key not in args:
            return f"missing required argument {key!r}"
    if schema.get("additionalProperties") is False:
        extra = [k for k in args if k not in props]
        if extra:
            return f"unexpected argument(s): {', '.join(sorted(extra))}"
    for key, value in args.items():
        prop = props.get(key)
        if not isinstance(prop, dict):
            continue
        expected = prop.get("type")
        if isinstance(expected, str) and expected in _JSON_TYPES:
            ok = isinstance(value, _JSON_TYPES[expected])
            if expected in ("integer", "number") and isinstance(value, bool):
                ok = False
            if not ok and not (value is None and key not in (schema.get("required") or [])):
                return f"argument {key!r} must be of type {expected}"
        if "enum" in prop and value not in prop["enum"]:
            return f"argument {key!r} must be one of {prop['enum']}"
    return None


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"duplicate tool {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def subset(self, names: Iterable[str]) -> "ToolRegistry":
        """Only the named tools, in the given order; unknown names raise."""
        missing = [n for n in names if n not in self._tools]
        if missing:
            raise ValueError(f"unknown tool(s): {', '.join(missing)}")
        return ToolRegistry(self._tools[n] for n in names)

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Run one call through policy, validation and the handler. Expected
        failures come back as error results; BudgetExceeded propagates."""
        def error(message: str) -> ToolResult:
            return ToolResult(call_id=call.id, name=call.name, content=message, is_error=True)

        if call.invalid is not None:
            return error(f"invalid tool arguments (not valid JSON): {call.invalid}")
        tool = self._tools.get(call.name)
        if tool is None:
            return error(f"unknown tool {call.name!r}; available: {', '.join(self._tools)}")
        try:
            ctx.policy.check_tool(tool.name, tool.risk)
            problem = validate_args(tool.input_schema, call.arguments)
            if problem:
                return error(problem)
            output = tool.handler(dict(call.arguments), ctx)
        except PolicyViolation as exc:
            ctx.events.emit("policy_denied", tool=call.name, reason=str(exc))
            return error(f"denied by policy: {exc}")
        except BudgetExceeded:
            raise
        except (ToolError, AgentKitError) as exc:
            return error(redact(str(exc), ctx.secrets))
        except Exception as exc:  # a bug in a tool must not kill a long run
            return error(redact(f"tool failed: {type(exc).__name__}: {exc}", ctx.secrets))
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        text = truncate(redact(text, ctx.secrets), ctx.max_output_chars)
        if tool.untrusted_output:
            text = wrap_untrusted(text, f"tool:{tool.name}")
        return ToolResult(call_id=call.id, name=call.name, content=text)


# Helpers passed only when a domain function names them explicitly (not
# through **kwargs, so functions that forward **kwargs are not surprised).
NAMED_HELPERS = ("resolve_path", "ledger", "events")


def wrap_function(fn: Callable[..., Any]) -> Handler:
    """Adapt a domain function fn(workspace, *, fetch=None, run=None, **args)
    to a handler, passing only the helpers it accepts (fetch and run also to
    **kwargs; resolve_path, ledger and events only by name)."""
    params = inspect.signature(fn).parameters
    takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())

    def handler(args: dict, ctx: ToolContext) -> Any:
        clash = [k for k in args if k in ("fetch", "run", *NAMED_HELPERS)]
        if clash:
            raise ToolError(f"argument name(s) reserved for the kit: {', '.join(clash)}")
        extra: dict[str, Any] = {}
        if takes_kwargs or "fetch" in params:
            extra["fetch"] = ctx.fetch
        if takes_kwargs or "run" in params:
            extra["run"] = ctx.run
        if "resolve_path" in params:
            def resolve_path(p: str, *, write: bool = False) -> Path:
                path = ctx.path(p, write=write)
                if write:
                    ctx.ledger.note_authored(ctx.rel(path))
                return path
            extra["resolve_path"] = resolve_path
        if "ledger" in params:
            extra["ledger"] = ctx.ledger
        if "events" in params:
            extra["events"] = ctx.events
        return fn(ctx.workspace, **extra, **args)

    return handler


def tools_from_defs(defs: Iterable[Any]) -> list[Tool]:
    """Build Tools from a specialist's TOOL_DEFS: Tool instances, or dicts
    with name, description, input_schema, function, risk ("read" if
    omitted), untrusted_output (True if omitted: domain tools usually return
    customer or fetched text)."""
    out = []
    for d in defs:
        if isinstance(d, Tool):
            out.append(d)
            continue
        fn = d.get("function") or d.get("fn")
        if fn is None:
            raise ValueError(f"tool def {d.get('name')!r} has no function "
                             "(use Tool(...) for a raw handler(args, ctx))")
        out.append(Tool(name=d["name"], description=d.get("description", ""),
                        input_schema=d.get("input_schema") or {"type": "object", "properties": {}},
                        handler=wrap_function(fn), risk=d.get("risk", "read"),
                        untrusted_output=bool(d.get("untrusted_output", True))))
    return out


__all__ = ["CommandResult", "FetchResult", "MAX_CAPTURE_BYTES", "MAX_OUTPUT_CHARS", "NAMED_HELPERS",
           "SAFE_ENV_KEYS", "TOOL_RISKS", "Tool", "ToolContext", "ToolRegistry", "read_capped", "resolve_public", "scrubbed_env",
           "subprocess_runner", "tools_from_defs", "truncate", "urllib_transport",
           "validate_args", "wrap_function"]
