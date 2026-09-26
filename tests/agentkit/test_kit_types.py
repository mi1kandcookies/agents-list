"""Core kit types round-trip through JSON; ModelRef parsing; ScriptedAdapter."""
import json

import pytest

from agentkit.llm import ModelRef, ScriptedAdapter
from agentkit.types import (AcceptanceCriterion, Artifact, Brief, CheckResult, HumanReview,
                            Message, MilestoneSpec, Submission, ToolCall, ToolResult, Usage)


def _roundtrip(obj):
    return type(obj).from_dict(json.loads(json.dumps(obj.to_dict())))


def test_message_roundtrip_keeps_calls_results_and_raw():
    msg = Message(role="assistant", text="hi",
                  tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a"})],
                  raw={"provider": "anthropic", "model": "m", "content": [{"type": "text"}]})
    assert _roundtrip(msg) == msg
    tool = Message.tool([ToolResult(call_id="c1", name="read_file", content="x", is_error=True)])
    assert _roundtrip(tool) == tool


def test_brief_and_milestone_roundtrip():
    m = MilestoneSpec(id="m1", title="Plan", deliverables=["deliverables/m1/plan.md"],
                      acceptance=[AcceptanceCriterion(check="file_exists",
                                                      params={"path": "deliverables/m1/plan.md"}),
                                  AcceptanceCriterion(check="human_signoff", kind="human")],
                      hours=(1, 3))
    brief = Brief(engagement_id="e1", specialist="market-research", objective="Size it",
                  intake={"market": "x"}, milestones=[m], answers={"q1": "yes"})
    back = _roundtrip(brief)
    assert back == brief
    assert back.milestone("m1").hours == (1.0, 3.0)
    assert back.milestone("nope") is None


def test_submission_roundtrip_and_unknown_keys_ignored():
    sub = Submission(engagement_id="e1", milestone_id="m1", status="ready_for_review",
                     summary="ok", artifacts=[Artifact("deliverables/m1/a.md", "ab" * 32, 10, "text/markdown")],
                     check_results=[CheckResult("file_exists", True), CheckResult("human_signoff", None, "human")],
                     human_review=HumanReview(required=True, reviewer_role="attorney"),
                     usage=Usage(10, 5, cost_usd=0.01), evidence_hash="0x" + "0" * 64)
    assert _roundtrip(sub) == sub
    data = sub.to_dict()
    data["future_field"] = 1
    assert Submission.from_dict(data) == sub


def test_usage_addition_and_cost():
    total = Usage(10, 5) + Usage(1, 1, cache_read_tokens=4, cost_usd=0.5)
    assert (total.input_tokens, total.output_tokens, total.cache_read_tokens) == (11, 6, 4)
    assert total.cost_usd == 0.5
    assert (Usage() + Usage()).cost_usd is None
    assert total.total_tokens == 21


@pytest.mark.parametrize("ref,provider,model", [
    ("anthropic:claude-opus-5", "anthropic", "claude-opus-5"),
    ("ollama:qwen3:32b", "ollama", "qwen3:32b"),
    (" OpenAI : gpt-x ", "openai", "gpt-x"),
])
def test_model_ref_parse(ref, provider, model):
    parsed = ModelRef.parse(ref)
    assert (parsed.provider, parsed.model) == (provider, model)


@pytest.mark.parametrize("bad", ["", "claude-opus-5", "nope:model", "anthropic:"])
def test_model_ref_rejects_bad_refs(bad):
    with pytest.raises(ValueError):
        ModelRef.parse(bad)


def test_scripted_adapter_plays_a_tool_plan_then_exhausts():
    adapter = ScriptedAdapter.from_tool_plan([
        ("read_file", {"path": "a"}),
        [("list_files", {}), ("read_file", {"path": "b"})],
        "done",
    ])
    first = adapter.complete(system="s", messages=[Message.user("go")], tools=[])
    assert first.stop_reason == "tool_use" and first.tool_calls[0].name == "read_file"
    second = adapter.complete(system="s", messages=[], tools=[])
    assert [c.name for c in second.tool_calls] == ["list_files", "read_file"]
    assert len({c.id for c in first.tool_calls + second.tool_calls}) == 3
    assert adapter.complete(system="s", messages=[], tools=[]).text == "done"
    assert adapter.complete(system="s", messages=[], tools=[]).text == "(script exhausted)"
    assert adapter.calls[0]["messages"][0]["text"] == "go"
