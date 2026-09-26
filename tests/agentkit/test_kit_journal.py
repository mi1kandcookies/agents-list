"""Events, the append-only journal with checkpoints, and the claim ledger."""
import io
import json

import pytest

from agentkit.errors import ToolError
from agentkit.events import JsonlSink, MemorySink, MultiSink
from agentkit.journal import Checkpoint, Journal
from agentkit.ledger import Ledger, normalize_text
from agentkit.types import Message, ToolCall, Usage


def test_memory_and_jsonl_sinks_redact():
    secret = "sk-" + "abc123def456"
    buf = io.StringIO()
    mem = MemorySink()
    sink = MultiSink(mem, JsonlSink(buf, secrets=[secret]))
    sink.emit("tool_result", content=f"key={secret}")
    assert mem.types == ["tool_result"] and mem.of_type("tool_result")[0].data["content"].endswith(secret)
    line = json.loads(buf.getvalue())
    assert line["type"] == "tool_result" and secret not in buf.getvalue()


def test_secrets_with_json_escaped_characters_are_redacted(tmp_path):
    secret = 'pa"ss\\w0rd-' + "9913"
    buf = io.StringIO()
    journal = Journal.for_workspace(tmp_path, secrets=[secret])
    sink = MultiSink(JsonlSink(buf, secrets=[secret]), journal)
    sink.emit("tool_call", name="login", arguments={"password": secret, "nested": [f"x{secret}y"]})
    written = buf.getvalue() + journal.path.read_text(encoding="utf-8")
    assert "9913" not in written and written.count("[REDACTED]") == 4
    assert json.loads(buf.getvalue())["data"]["arguments"]["nested"] == ["x[REDACTED]y"]


def test_journal_is_append_only(tmp_path):
    j = Journal.for_workspace(tmp_path)
    j.emit("run_started", milestone="m1")
    j.emit("run_finished", status="submitted")
    j2 = Journal.for_workspace(tmp_path)
    j2.emit("run_started", milestone="m1")
    assert [e.type for e in j2.entries()] == ["run_started", "run_finished", "run_started"]


def test_checkpoint_roundtrip_and_clear(tmp_path):
    j = Journal.for_workspace(tmp_path)
    assert j.load_checkpoint("m1") is None
    cp = Checkpoint(milestone_id="m1/../x", step=3,
                    messages=[Message.user("go"),
                              Message(role="assistant", tool_calls=[ToolCall("c1", "read_file", {"path": "a"})])],
                    usage=Usage(10, 5, cost_usd=None), nudged=True, questions=["Which?"],
                    submitted={"summary": "s"})
    j.save_checkpoint(cp)
    assert j.checkpoint_path("m1/../x").parent == tmp_path / ".agentkit" / "checkpoints"
    back = j.load_checkpoint("m1/../x")
    assert back == cp
    j.clear_checkpoint("m1/../x")
    assert j.load_checkpoint("m1/../x") is None


SOURCE_TEXT = "Revenue grew\n  to $12.4 million in FY2025, up 18%.“Strong” demand."


def test_ledger_records_and_verifies(tmp_path):
    led = Ledger(tmp_path)
    s = led.add_source("https://example.org/r", "Report", SOURCE_TEXT)
    assert s.id == "S1" and led.add_source("https://example.org/r", "Report", SOURCE_TEXT) is s
    c = led.add_claim("Revenue was $12.4M", "S1", "Revenue grew to $12.4 million", "p1")
    assert c.id == "C1"
    led.add_claim("Demand was strong", "S1", '"Strong" demand')        # curly quotes normalized
    assert Ledger(tmp_path).claims[1].id == "C2"                       # persisted
    assert Ledger(tmp_path).verify() == []


@pytest.mark.parametrize("source,quote,match", [
    ("S1", "Revenue grew to $13.0 million", "not found"),
    ("S9", "Revenue grew to $12.4 million", "unknown source"),
    ("S1", "grew", "at least"),
])
def test_ledger_rejects_forged_quotes(tmp_path, source, quote, match):
    led = Ledger(tmp_path)
    led.add_source("inputs/r.txt", "r", SOURCE_TEXT)
    with pytest.raises(ToolError, match=match):
        led.add_claim("x", source, quote)
    with pytest.raises(ToolError):
        led.add_claim("  ", "S1", "Revenue grew to $12.4 million")


def test_ledger_verify_catches_tampering(tmp_path):
    led = Ledger(tmp_path)
    led.add_source("inputs/r.txt", "r", SOURCE_TEXT)
    led.add_claim("ok", "S1", "up 18%.\"Strong\" demand")
    data = json.loads(led.path.read_text(encoding="utf-8"))
    data["claims"].append({"id": "C2", "text": "forged", "source": "S1",
                           "quote": "Revenue fell sharply", "location": ""})
    led.path.write_text(json.dumps(data), encoding="utf-8")
    problems = Ledger(tmp_path).verify()
    assert len(problems) == 1 and problems[0].startswith("C2:")
    led.snapshot_path("S1").write_text("changed", encoding="utf-8")
    assert len(Ledger(tmp_path).verify()) == 2


def test_ledger_detects_an_edited_snapshot_by_hash(tmp_path):
    led = Ledger(tmp_path)
    src = led.add_source("https://example.org/r", "r", SOURCE_TEXT, kind="web")
    assert src.kind == "web" and len(src.sha256) == 64
    led.add_claim("ok", "S1", "Revenue grew to $12.4 million")
    # rewrite the snapshot to contain a fabricated quote, then a claim quoting it
    led.snapshot_path("S1").write_text(SOURCE_TEXT + " Revenue will triple in 2026.", encoding="utf-8")
    data = json.loads(led.path.read_text(encoding="utf-8"))
    data["claims"].append({"id": "C2", "text": "forged", "source": "S1",
                           "quote": "Revenue will triple in 2026", "location": ""})
    led.path.write_text(json.dumps(data), encoding="utf-8")
    problems = Ledger(tmp_path).verify()
    assert len(problems) == 2 and all("changed after it was recorded" in p for p in problems)


def test_ledger_refuses_authored_sources_and_loads_older_records(tmp_path):
    led = Ledger(tmp_path)
    led.add_source("workspace:repo/notes.md", "notes", "The agent's own notes say 90% growth.",
                   kind="customer")
    led.add_claim("growth", "S1", "own notes say 90% growth")
    assert Ledger(tmp_path).verify() == []
    led.note_authored("repo/notes.md")
    assert Ledger(tmp_path).authored == ["repo/notes.md"]
    assert "written during the engagement" in Ledger(tmp_path).verify()[0]
    # the same file under another case (a case-insensitive filesystem) is still authored
    assert Ledger(tmp_path).is_authored("repo/NOTES.md") and Ledger(tmp_path).is_authored("Repo/Notes.MD")
    assert not Ledger(tmp_path).is_authored("repo/notes.txt")
    # a ledger written before sources carried kind/sha256 still loads and verifies
    old = {"sources": [{"id": "S1", "uri": "inputs/r.txt", "title": "r", "retrieved_at": "t",
                        "future_field": 1}],
           "claims": [{"id": "C1", "text": "x", "source": "S1", "quote": "Revenue grew to $12.4 million",
                       "location": ""}]}
    other = tmp_path / "old"
    (other / ".agentkit" / "sources").mkdir(parents=True)
    (other / ".agentkit" / "ledger.json").write_text(json.dumps(old), encoding="utf-8")
    (other / ".agentkit" / "sources" / "S1.txt").write_text(SOURCE_TEXT, encoding="utf-8")
    assert Ledger(other).verify() == []
    with pytest.raises(ValueError):
        led.add_source("x", "x", "text", kind="made-up")


def test_normalize_text():
    assert normalize_text("  a  b\n\tc ’ ") == "a b c '"
