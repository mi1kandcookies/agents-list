"""Built-in tools: workspace jail, shell allowlist, egress, documents, ledger, platform."""
import json
import os
import sys
import zipfile

import pytest

from agentkit.events import MemorySink
from agentkit.policy import PolicyGate, executable_name
from agentkit.tools import ToolContext, builtin_registry, tools_from_defs, validate_args
from agentkit.tools.base import scrubbed_env, truncate
from agentkit.tools.documents import docx_to_text, html_to_text
from agentkit.types import Brief, ToolCall


# The running interpreter's basename (python, python3.12, ...) is what gets allowlisted.
PY = executable_name(sys.executable)


def _ctx(ws, **gate_kwargs):
    events = MemorySink()
    ctx = ToolContext(workspace=ws, policy=PolicyGate(**gate_kwargs), events=events,
                      brief=Brief("e1", "demo", "obj", answers={"Which year?": "FY2025"}))
    return ctx, events


def _call(ctx, name, **args):
    return builtin_registry().execute(ToolCall(id="c1", name=name, arguments=args), ctx)


def _json(result):
    """The JSON payload of a tool result, inside or outside an <untrusted> wrapper."""
    text = result.content
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "notes.txt").write_text("alpha\nbeta gamma\nalpha again\n", encoding="utf-8")
    return root


# --- workspace -----------------------------------------------------------------

def test_write_read_edit_list_search(ws):
    ctx, _ = _ctx(ws)
    r = _call(ctx, "write_file", path="deliverables/m1/report.md", content="# Title\nhello world\n")
    assert not r.is_error and json.loads(r.content)["path"] == "deliverables/m1/report.md"
    r = _call(ctx, "read_file", path="deliverables/m1/report.md")
    assert "hello world" in r.content and r.content.startswith("<untrusted")
    assert "2: hello world" in _call(ctx, "read_file", path="deliverables/m1/report.md",
                                     offset=2, limit=1).content
    assert not _call(ctx, "edit_file", path="deliverables/m1/report.md",
                     old_text="hello", new_text="goodbye").is_error
    assert "goodbye world" in (ws / "deliverables/m1/report.md").read_text(encoding="utf-8")
    assert "not found" in _call(ctx, "edit_file", path="deliverables/m1/report.md",
                                old_text="nope", new_text="x").content
    listed = _call(ctx, "list_files")
    assert listed.content.startswith("<untrusted")          # file names are customer data
    assert {f["path"] for f in _json(listed)["files"]} == {"inputs/notes.txt", "deliverables/m1/report.md"}
    found = _call(ctx, "search_files", pattern="ALPHA", path="inputs")
    assert "inputs/notes.txt:1: alpha" in found.content and "inputs/notes.txt:3:" in found.content


def test_edit_file_ambiguous_and_replace_all(ws):
    ctx, _ = _ctx(ws)
    _call(ctx, "write_file", path="a.txt", content="x x x")
    assert "3 times" in _call(ctx, "edit_file", path="a.txt", old_text="x", new_text="y").content
    assert not _call(ctx, "edit_file", path="a.txt", old_text="x", new_text="y", replace_all=True).is_error
    assert (ws / "a.txt").read_text(encoding="utf-8") == "y y y"


@pytest.mark.parametrize("name,args", [
    ("write_file", {"path": "inputs/notes.txt", "content": "overwrite"}),
    ("write_file", {"path": "../escape.txt", "content": "x"}),
    ("read_file", {"path": ".agentkit/ledger.json"}),
    ("read_file", {"path": "/etc/passwd"}),
    ("edit_file", {"path": "inputs/notes.txt", "old_text": "alpha", "new_text": "x"}),
])
def test_path_escapes_denied(ws, name, args):
    ctx, events = _ctx(ws)
    r = _call(ctx, name, **args)
    assert r.is_error and "denied by policy" in r.content
    assert events.of_type("policy_denied")
    assert (ws / "inputs/notes.txt").read_text(encoding="utf-8").startswith("alpha")
    assert not (ws.parent / "escape.txt").exists()


def test_list_files_hides_internal_dir(ws):
    ctx, _ = _ctx(ws)
    (ws / ".agentkit").mkdir()
    (ws / ".agentkit" / "journal.jsonl").write_text("{}", encoding="utf-8")
    assert ".agentkit" not in _call(ctx, "list_files").content


def _link_dir(link, target):
    """A directory junction on Windows, a symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this system")


def test_walks_do_not_follow_links_or_junctions_out_of_the_workspace(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE-SECRET value=hunter2\n", encoding="utf-8")
    (ws / "repo").mkdir()
    _link_dir(ws / "repo" / "vendor", outside)
    ctx, _ = _ctx(ws)
    assert "secret.txt" not in _call(ctx, "list_files").content
    assert "OUTSIDE-SECRET" not in _call(ctx, "search_files", pattern="OUTSIDE").content
    assert _call(ctx, "read_file", path="repo/vendor/secret.txt").is_error


# --- registry ------------------------------------------------------------------

def test_registry_errors_and_validation(ws):
    ctx, _ = _ctx(ws, tools=["read_file"])
    reg = builtin_registry()
    assert "not enabled" in reg.execute(ToolCall("c", "write_file", {"path": "a", "content": ""}), ctx).content
    assert "unknown tool" in reg.execute(ToolCall("c", "nope", {}), ctx).content
    assert "not valid JSON" in reg.execute(ToolCall("c", "read_file", {}, invalid="{bad"), ctx).content
    assert "missing required" in reg.execute(ToolCall("c", "read_file", {}), ctx).content
    assert validate_args({"properties": {"n": {"type": "integer"}}}, {"n": True})
    assert validate_args({"properties": {"a": {"enum": ["x"]}}}, {"a": "y"})
    assert validate_args({"additionalProperties": False, "properties": {}}, {"z": 1})
    assert validate_args({}, [1]) == "arguments must be a JSON object"


def test_tool_crash_becomes_error_and_secrets_redacted(ws):
    secret = "sk-" + "zz" * 10

    def boom(workspace, **args):
        raise RuntimeError(f"leaked {secret}")

    def echo(workspace, *, value):
        return {"value": value, "ws": workspace.name}

    tools = tools_from_defs([
        {"name": "boom", "function": boom, "input_schema": {"type": "object", "properties": {}}},
        {"name": "echo", "function": echo, "risk": "read",
         "input_schema": {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}}},
    ])
    from agentkit.tools import ToolRegistry
    reg = ToolRegistry(tools)
    ctx, _ = _ctx(ws)
    ctx.env = {"OPENAI_API_KEY": secret}
    r = reg.execute(ToolCall("c", "boom", {}), ctx)
    assert r.is_error and "RuntimeError" in r.content and secret not in r.content
    echoed = reg.execute(ToolCall("c", "echo", {"value": "v"}), ctx)
    assert echoed.content.startswith('<untrusted source="tool:echo">')   # domain output wrapped by default
    assert _json(echoed) == {"value": "v", "ws": "ws"}
    with pytest.raises(ValueError):
        ToolRegistry(tools + tools)


def test_domain_tools_get_jailed_path_ledger_and_events_helpers(ws):
    from agentkit.tools import ToolRegistry

    def save(workspace, *, resolve_path, ledger, events, path, text):
        target = resolve_path(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        events.emit("progress", message="saved")
        src = ledger.add_source("https://data.example.org/x", "x", "fetched figures 12% growth")
        return {"saved": path, "source": src.id}

    def forwards(workspace, *, path, **rest):
        return {"rest": sorted(rest)}

    reg = ToolRegistry(tools_from_defs([
        {"name": "save", "function": save, "risk": "write", "untrusted_output": False,
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}}},
        {"name": "forwards", "function": forwards,
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ]))
    ctx, events = _ctx(ws)
    ok = reg.execute(ToolCall("c", "save", {"path": "deliverables/d.csv", "text": "a,b"}), ctx)
    assert json.loads(ok.content) == {"saved": "deliverables/d.csv", "source": "S1"}
    assert ctx.ledger.is_authored("deliverables/d.csv") and ctx.ledger.sources[0].kind == "tool"
    assert events.of_type("progress")
    for bad in ("inputs/notes.txt", ".agentkit/ledger.json", "../escape.csv"):
        r = reg.execute(ToolCall("c", "save", {"path": bad, "text": "x"}), ctx)
        assert r.is_error and "denied by policy" in r.content, bad
    assert (ws / "inputs/notes.txt").read_text(encoding="utf-8").startswith("alpha")
    # **kwargs functions get fetch/run as before, never the named helpers
    assert _json(reg.execute(ToolCall("c", "forwards", {"path": "a"}), ctx)) == {"rest": ["fetch", "run"]}
    assert "reserved" in reg.execute(ToolCall("c", "forwards", {"path": "a", "ledger": "x"}), ctx).content


@pytest.mark.parametrize("bad", [{"risk": "External"}, {"risk": "send"}, {"handler": lambda a, c: 1}])
def test_tool_defs_reject_unknown_risk_and_handler_alias(bad):
    d = {"name": "t", "function": lambda workspace: 1, **bad}
    if "handler" in bad:
        del d["function"]
    with pytest.raises(ValueError):
        tools_from_defs([d])


def test_truncate_keeps_head_and_tail():
    out = truncate("a" * 50 + "b" * 50, 30)
    assert out.startswith("a" * 20) and out.endswith("b" * 10) and "truncated" in out


# --- shell -----------------------------------------------------------------------

def test_run_command_allowlisted(ws, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "t" * 12)
    ctx, events = _ctx(ws, shell_allow=[PY])
    code = "import os,sys; print('ok', 'SUPER_SECRET_TOKEN' in os.environ); sys.exit(3)"
    r = _call(ctx, "run_command", argv=[sys.executable, "-c", code])
    data = json.loads(r.content[r.content.index("{"):r.content.rindex("}") + 1])
    assert data["exit_code"] == 3 and data["stdout"].strip() == "ok False"
    assert events.of_type("command")[0].data["exit_code"] == 3


def test_run_command_timeout_and_truncation(ws):
    ctx, _ = _ctx(ws, shell_allow=[PY], shell_timeout=1)
    ctx.max_output_chars = 100
    r = ctx.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=30)
    assert r.timed_out and r.exit_code == -1
    r = ctx.run([sys.executable, "-c", "print('x' * 1000)"])
    assert len(r.stdout) < 200 and "truncated" in r.stdout


def test_timeout_bounds_the_call_even_when_a_grandchild_holds_the_pipes(ws):
    import time
    ctx, _ = _ctx(ws, shell_allow=[PY], shell_timeout=2)
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']); time.sleep(20)")
    started = time.monotonic()
    r = ctx.run([sys.executable, "-c", code])
    assert r.timed_out and r.exit_code == -1
    assert time.monotonic() - started < 10


def test_background_children_do_not_outlive_the_command(ws, tmp_path):
    import time
    marker = tmp_path / "late-write.txt"
    ctx, _ = _ctx(ws, shell_allow=[PY])
    child = f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"
    code = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]); print('started')"
    r = ctx.run([sys.executable, "-c", code])
    assert r.exit_code == 0 and "started" in r.stdout
    time.sleep(3)
    assert not marker.exists()


def test_output_is_captured_with_a_byte_cap(ws, monkeypatch):
    from agentkit.tools import base
    monkeypatch.setattr(base, "MAX_CAPTURE_BYTES", 1000)
    ctx, _ = _ctx(ws, shell_allow=[PY])
    r = ctx.run([sys.executable, "-c", "print('x' * 200000); print('END')"])
    assert "bytes dropped" in r.stdout and r.stdout.rstrip().endswith("END")
    assert len(r.stdout) < 3000


def test_programs_are_found_on_path_never_in_the_workspace(ws, tmp_path, monkeypatch):
    from agentkit.errors import PolicyViolation
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))
    ctx, _ = _ctx(ws, shell_allow=[PY, "tool.bat"])
    assert ctx.run([PY, "-c", "print('from PATH')"]).stdout.strip() == "from PATH"
    (ws / "repo").mkdir()
    planted = ws / "repo" / os.path.basename(sys.executable)
    planted.write_bytes(b"not really a program")
    for argv0 in (str(planted), f"repo/{planted.name}", f"./repo/{planted.name}"):
        with pytest.raises(PolicyViolation, match="inside the workspace"):
            ctx.run([argv0, "-V"])
    batch = tmp_path / "tool.bat"
    batch.write_text("@echo off\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="batch file"):
        ctx.run([str(batch)])


@pytest.mark.parametrize("argv", [["rm", "-rf", "/"], ["bash", "-c", "python"], "python -V", []])
def test_run_command_denied(ws, argv):
    ctx, _ = _ctx(ws, shell_allow=[PY])
    r = _call(ctx, "run_command", argv=argv)
    assert r.is_error


def test_scrubbed_env_keeps_only_safe_keys():
    env = scrubbed_env({"PATH": "/bin", "OPENAI_API_KEY": "x", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}


# --- egress ----------------------------------------------------------------------

class FakeTransport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        return self.pages[url]


PAGE = (b"<html><head><title>Widget Report</title><script>evil()</script></head>"
        b"<body><p>The widget market was worth $4.2 billion in 2025.</p></body></html>")


def test_http_fetch_registers_source(ws):
    ctx, events = _ctx(ws, egress_mode="allowlist", egress_allow=["*.example.org"])
    ctx.transport = FakeTransport({"https://www.example.org/r": (200, {"Content-Type": "text/html"}, PAGE)})
    r = _call(ctx, "http_fetch", url="https://www.example.org/r")
    data = json.loads(r.content[r.content.index("{"):r.content.rindex("}") + 1])
    assert data["source_id"] == "S1" and data["title"] == "Widget Report"
    assert "evil" not in data["text"] and "$4.2 billion" in data["text"]
    claim = _call(ctx, "record_claim", text="Market $4.2B", source="S1",
                  quote="widget market was worth $4.2 billion")
    assert json.loads(claim.content)["cite_as"] == "[C1]"
    assert events.of_type("egress")[0].data["status"] == 200


def test_http_fetch_redirect_to_disallowed_host_denied(ws):
    ctx, _ = _ctx(ws, egress_mode="allowlist", egress_allow=["www.example.org"])
    ctx.transport = FakeTransport({
        "https://www.example.org/r": (302, {"Location": "https://evil.test/steal"}, b""),
        "https://evil.test/steal": (200, {}, b"gotcha"),
    })
    r = _call(ctx, "http_fetch", url="https://www.example.org/r")
    assert r.is_error and "egress allowlist" in r.content
    assert ctx.transport.calls == [("GET", "https://www.example.org/r")]


@pytest.mark.parametrize("url", ["https://evil.test/", "http://169.254.169.254/latest/meta-data",
                                 "file:///etc/passwd", "https://example.org.evil.test/"])
def test_http_fetch_non_allowlisted_denied(ws, url):
    ctx, _ = _ctx(ws, egress_mode="allowlist", egress_allow=["*.example.org"])
    ctx.transport = FakeTransport({})
    r = _call(ctx, "http_fetch", url=url)
    assert r.is_error and "denied by policy" in r.content and ctx.transport.calls == []


def test_fetch_size_cap_and_methods(ws):
    ctx, _ = _ctx(ws, egress_mode="allowlist", egress_allow=["a.test"])
    ctx.max_fetch_bytes = 10
    ctx.transport = FakeTransport({"https://a.test/": (200, {}, b"0123456789abcdef")})
    res = ctx.fetch("https://a.test/")
    assert res.truncated and res.body == b"0123456789"
    from agentkit.errors import PolicyViolation
    with pytest.raises(PolicyViolation):
        ctx.fetch("https://a.test/", method="DELETE")


def _fake_dns(monkeypatch, address):
    import socket

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_default_transport_refuses_names_that_resolve_to_private_addresses(ws, monkeypatch):
    # e.g. localtest.me -> 127.0.0.1, or a rebinding name -> 169.254.169.254
    for address in ("127.0.0.1", "169.254.169.254", "10.0.0.8", "::1"):
        _fake_dns(monkeypatch, address)
        ctx, events = _ctx(ws, egress_mode="allowlist", egress_allow=["*"])
        r = _call(ctx, "http_fetch", url="http://localtest.example/latest/meta-data/")
        assert r.is_error and "non-public address" in r.content, r.content
        assert not events.of_type("egress")


def test_default_transport_connects_to_the_checked_address_with_the_host_name(ws, monkeypatch):
    import http.server
    import threading

    from agentkit.tools import base

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers["Host"]
            body = b"<html><title>ok</title><p>pinned page</p></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _fake_dns(monkeypatch, "127.0.0.1")
        monkeypatch.setattr(base, "is_public_ip", lambda address: True)  # let loopback stand in
        port = server.server_address[1]
        ctx, _ = _ctx(ws, egress_mode="allowlist", egress_allow=["pinned.example"])
        res = ctx.fetch(f"http://pinned.example:{port}/page")
        assert res.status == 200 and b"pinned page" in res.body
        assert seen["host"] == f"pinned.example:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_read_capped_enforces_an_overall_deadline():
    from agentkit.errors import ToolError
    from agentkit.tools.base import read_capped

    class Drip:
        def read(self, n):
            return b"x"

    ticks = iter(range(100))
    with pytest.raises(ToolError, match="longer than"):
        read_capped(Drip(), 1000, deadline=5, clock=lambda: next(ticks))

    class Big:
        def read(self, n):
            return b"y" * n
    assert len(read_capped(Big(), 10, deadline=float("inf"))) == 11     # limit + 1 marks truncation


def test_cross_origin_redirect_drops_credentials_and_body(ws):
    ctx, _ = _ctx(ws, egress_mode="allowlist", egress_allow=["a.test", "b.test"])
    seen = []

    def transport(method, url, headers, body, timeout):
        seen.append((method, url, dict(headers), body))
        if url == "https://a.test/start":
            return 307, {"Location": "https://a.test/same"}, b""
        if url == "https://a.test/same":
            return 308, {"Location": "https://b.test/other"}, b""
        return 200, {}, b"done"

    ctx.transport = transport
    ctx.fetch("https://a.test/start", method="POST", body="secret-form",
              headers={"Authorization": "Bearer abc", "X-Api-Key": "k", "User-Agent": "ua/1"})
    same, cross = seen[1], seen[2]
    assert same[0] == "POST" and same[3] == b"secret-form" and same[2]["Authorization"] == "Bearer abc"
    assert cross[0] == "GET" and cross[3] is None
    assert cross[2] == {"User-Agent": "ua/1"}


def test_web_search_not_configured_then_pluggable(ws):
    ctx, _ = _ctx(ws)
    assert "not configured" in _call(ctx, "web_search", query="x").content
    ctx.services["search"] = lambda q, n: [{"title": "T", "url": "https://a.test", "snippet": q}] * 9
    data = _call(ctx, "web_search", query="widgets", limit=2).content
    assert data.count("https://a.test") == 2


# --- documents -------------------------------------------------------------------

def _docx(path, body_xml):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body_xml}</w:body></w:document>')


def test_read_document_docx_html_json(ws):
    _docx(ws / "inputs" / "msa.docx",
          "<w:p><w:r><w:t>1. Term.</w:t></w:r><w:r><w:tab/><w:t>Two years</w:t></w:r></w:p>"
          "<w:p><w:del><w:r><w:delText>old</w:delText></w:r></w:del><w:r><w:t>Renewal</w:t></w:r></w:p>"
          "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Fee</w:t></w:r></w:p></w:tc>"
          "<w:tc><w:p><w:r><w:t>$10</w:t></w:r></w:p></w:tc></w:tr></w:tbl>")
    assert docx_to_text(ws / "inputs" / "msa.docx") == "1. Term.\tTwo years\nRenewal\nFee | $10"
    ctx, _ = _ctx(ws)
    assert "Two years" in _call(ctx, "read_document", path="inputs/msa.docx").content
    (ws / "inputs" / "p.html").write_text("<title>T</title><p>Hi</p><style>x{}</style>", encoding="utf-8")
    assert "T\n\nHi" in _call(ctx, "read_document", path="inputs/p.html").content
    (ws / "inputs" / "d.json").write_text('{"a":1}', encoding="utf-8")
    assert '"a": 1' in _call(ctx, "read_document", path="inputs/d.json").content
    (ws / "inputs" / "bad.docx").write_bytes(b"not a zip")
    assert "not a readable" in _call(ctx, "read_document", path="inputs/bad.docx").content
    (ws / "inputs" / "x.exe").write_bytes(b"MZ")
    assert "unsupported" in _call(ctx, "read_document", path="inputs/x.exe").content


def test_html_to_text():
    assert html_to_text("<title> A  B </title><div>x</div><div>y</div>") == ("A B", "x\ny")


# --- ledger + platform tools -------------------------------------------------------

def test_record_source_from_workspace_file_and_forged_quote(ws):
    ctx, _ = _ctx(ws)
    src = json.loads(_call(ctx, "record_source", path="inputs/notes.txt").content)
    assert src == {"source_id": "S1", "uri": "workspace:inputs/notes.txt"}
    ok = _call(ctx, "record_claim", text="beta", source="S1", quote="beta gamma alpha again")
    assert not ok.is_error
    forged = _call(ctx, "record_claim", text="delta", source="S1", quote="delta epsilon zeta")
    assert forged.is_error and "not found verbatim" in forged.content


def test_record_source_refuses_files_the_agent_wrote_or_non_client_paths(ws):
    ctx, events = _ctx(ws)
    notes = "Acme revenue grew 400% in 2025 and it holds 97% share."
    for path in ("deliverables/notes.md", "notes.md", "repo/NOTES.md"):
        assert not _call(ctx, "write_file", path=path, content=notes).is_error
        r = _call(ctx, "record_source", path=path)
        assert r.is_error and "denied by policy" in r.content, (path, r.content)
    # a client repo file that was edited during the run is not a source either
    (ws / "repo" / "README.md").write_text("Client readme text for the project.", encoding="utf-8")
    assert not _call(ctx, "edit_file", path="repo/README.md", old_text="Client", new_text="Acme").is_error
    assert "written during" in _call(ctx, "record_source", path="repo/README.md").content
    (ws / "repo" / "LICENSE").write_text("Apache License, Version 2.0", encoding="utf-8")
    assert not _call(ctx, "record_source", path="repo/LICENSE").is_error
    assert len(events.of_type("policy_denied")) == 4
    assert ctx.ledger.sources[0].kind == "customer"


def test_platform_tools(ws):
    ctx, events = _ctx(ws)
    assert "FY2025" in _call(ctx, "ask_client", question="Which year?").content
    assert "recorded" in _call(ctx, "ask_client", question="Which region?").content
    assert ctx.state["questions"] == ["Which region?"]
    _call(ctx, "post_progress", message="halfway")
    assert events.of_type("progress")[0].data["message"] == "halfway"
    assert "artifact not found" in _call(ctx, "submit_milestone", summary="s", artifacts=["nope.md"]).content
    assert "at least one" in _call(ctx, "submit_milestone", summary="s", artifacts=[]).content
    assert "denied" in _call(ctx, "submit_milestone", summary="s", artifacts=["../x"]).content
    assert not _call(ctx, "submit_milestone", summary="s", artifacts=["inputs/notes.txt"]).is_error
    assert ctx.state["submitted"] == {"summary": "s", "artifacts": ["inputs/notes.txt"]}
