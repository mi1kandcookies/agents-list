"""Runner: append-only history, one tool message per turn, limits, stuck
detection, nudges, pause handling, checkpoints and resume."""
import pytest

from agentkit.errors import BudgetExceeded
from agentkit.events import MemorySink
from agentkit.journal import Checkpoint, Journal
from agentkit.llm import ScriptedAdapter
from agentkit.loop import Limits, Runner
from agentkit.policy import PolicyGate
from agentkit.tools import ToolContext, builtin_registry
from agentkit.types import ModelResponse, ToolCall, Usage

REPORT = "deliverables/m1/report.md"


def _setup(tmp_path, adapter, **limits):
    events = MemorySink()
    journal = Journal.for_workspace(tmp_path)
    ctx = ToolContext(workspace=tmp_path, policy=PolicyGate(), events=events)
    runner = Runner(adapter, builtin_registry(), events=events, journal=journal,
                    limits=Limits(**limits) if limits else None)
    return runner, ctx, events, journal


def _run(runner, ctx, **kw):
    return runner.run(ctx, system="sys", task="Write the report.", milestone_id="m1", **kw)


HAPPY = [
    [("write_file", {"path": REPORT, "content": "# Report\n"}),
     ("post_progress", {"message": "drafted"})],
    ("submit_milestone", {"summary": "Report done", "artifacts": [REPORT]}),
]


def test_happy_path_one_tool_message_per_turn(tmp_path):
    adapter = ScriptedAdapter.from_tool_plan(HAPPY)
    runner, ctx, events, journal = _setup(tmp_path, adapter)
    out = _run(runner, ctx)
    assert out.status == "submitted" and out.summary == "Report done"
    assert out.artifacts == [REPORT] and out.steps == 2
    assert out.usage.input_tokens == 200 and out.usage.cost_usd is None
    # second model call saw: user task, assistant (2 calls), ONE tool message with 2 results
    hist = adapter.calls[1]["messages"]
    assert [m["role"] for m in hist] == ["user", "assistant", "tool"]
    assert len(hist[2]["tool_results"]) == 2
    assert adapter.calls[0]["system"] == "sys" and "submit_milestone" in adapter.calls[0]["tools"]
    assert events.types[0] == "run_started" and events.types[-1] == "run_finished"
    assert journal.load_checkpoint("m1").status == "submitted"


def test_history_is_append_only(tmp_path):
    adapter = ScriptedAdapter.from_tool_plan([("list_files", {}), ("list_files", {"path": "."}),
                                              ("submit_milestone", {"summary": "s", "artifacts": []})])
    runner, ctx, _, _ = _setup(tmp_path, adapter)
    _run(runner, ctx)
    for earlier, later in zip(adapter.calls, adapter.calls[1:]):
        assert later["messages"][:len(earlier["messages"])] == earlier["messages"]


def test_nudge_once_then_no_submission(tmp_path):
    adapter = ScriptedAdapter.from_tool_plan(["I think I'm done.", "Still done."])
    runner, ctx, events, _ = _setup(tmp_path, adapter)
    out = _run(runner, ctx)
    assert out.status == "no_submission" and out.summary == "Still done."
    assert len(events.of_type("nudge")) == 1
    assert "submit_milestone" in adapter.calls[1]["messages"][-1]["text"]


def test_nudge_then_submit(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    adapter = ScriptedAdapter.from_tool_plan(["done?", ("submit_milestone", {"summary": "ok", "artifacts": ["a.md"]})])
    runner, ctx, _, _ = _setup(tmp_path, adapter)
    assert _run(runner, ctx).status == "submitted"


def test_stuck_detector_nudges_then_stops(tmp_path):
    adapter = ScriptedAdapter.from_tool_plan([("read_file", {"path": "missing.md"})] * 6)
    runner, ctx, events, _ = _setup(tmp_path, adapter)
    out = _run(runner, ctx)
    assert out.status == "stuck" and out.steps == 5
    nudges = events.of_type("nudge")
    assert [n.data["reason"] for n in nudges] == ["stuck"]
    assert adapter.calls[3]["messages"][-1]["role"] == "user"
    assert adapter.calls[3]["messages"][-2]["role"] == "tool"


def test_pause_continues_without_nudge(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    pause = ModelResponse(text="", tool_calls=[], stop_reason="pause", usage=Usage(1, 1), model="m",
                          raw={"provider": "scripted", "model": "m", "content": []})
    submit = ModelResponse(text="", tool_calls=[ToolCall("c1", "submit_milestone",
                                                         {"summary": "s", "artifacts": ["a.md"]})],
                           stop_reason="tool_use", usage=Usage(1, 1), model="m")
    runner, ctx, events, _ = _setup(tmp_path, ScriptedAdapter([pause, submit]))
    assert _run(runner, ctx).status == "submitted"
    assert not events.of_type("nudge")


def test_refusal_stops(tmp_path):
    refuse = ModelResponse(text="I can't help", tool_calls=[], stop_reason="refusal", usage=Usage(), model="m")
    runner, ctx, _, _ = _setup(tmp_path, ScriptedAdapter([refuse]))
    assert _run(runner, ctx).status == "refused"


def test_cut_off_reply_gets_continue_prompt(tmp_path):
    cut = ModelResponse(text="partial", tool_calls=[], stop_reason="max_tokens", usage=Usage(), model="m")
    adapter = ScriptedAdapter([cut, cut, cut])
    runner, ctx, events, _ = _setup(tmp_path, adapter, max_steps=3)
    out = _run(runner, ctx)
    assert out.status == "budget_exceeded" and out.limit == "max_steps"
    assert [n.data["reason"] for n in events.of_type("nudge")] == ["cut_off"] * 3


@pytest.mark.parametrize("limits,usage,which", [
    ({"max_steps": 2}, Usage(1, 1), "max_steps"),
    ({"max_tokens": 250}, Usage(100, 50), "max_tokens"),
    ({"max_usd": 1.0}, Usage(1, 1, cost_usd=0.6), "max_usd"),
])
def test_limits_raise_budget_exceeded(tmp_path, limits, usage, which):
    adapter = ScriptedAdapter.from_tool_plan([("list_files", {"path": str(i)}) for i in range(10)],
                                             usage_per_step=usage)
    runner, ctx, events, _ = _setup(tmp_path, adapter, **limits)
    out = _run(runner, ctx)
    assert out.status == "budget_exceeded" and out.limit == which
    assert events.of_type("budget_exceeded")[0].data["limit"] == which
    with pytest.raises(BudgetExceeded):
        runner.check_limits(Checkpoint("m1", step=out.steps, usage=out.usage), 0)


def test_wall_clock_limit(tmp_path):
    ticks = iter(range(0, 10_000, 100))
    adapter = ScriptedAdapter.from_tool_plan([("list_files", {"path": str(i)}) for i in range(50)])
    runner, ctx, _, _ = _setup(tmp_path, adapter, max_wall_minutes=5)
    runner.clock = lambda: float(next(ticks))
    out = _run(runner, ctx)
    assert out.status == "budget_exceeded" and out.limit == "max_wall_minutes"


def test_unknown_cost_never_trips_usd_limit(tmp_path):
    adapter = ScriptedAdapter.from_tool_plan([("list_files", {"path": str(i)}) for i in range(3)])
    runner, ctx, _, _ = _setup(tmp_path, adapter, max_usd=0.0001)
    assert _run(runner, ctx).status == "no_submission"


def test_checkpoint_each_step_and_resume(tmp_path):
    plan = [("write_file", {"path": REPORT, "content": "# R\n"}),
            ("list_files", {}),
            ("submit_milestone", {"summary": "done", "artifacts": [REPORT]})]
    first = ScriptedAdapter.from_tool_plan(plan[:2])
    runner, ctx, _, journal = _setup(tmp_path, first, max_steps=2)
    out = _run(runner, ctx)
    assert out.status == "budget_exceeded"
    cp = journal.load_checkpoint("m1")
    assert cp.step == 2 and [m.role for m in cp.messages] == ["user", "assistant", "tool", "assistant", "tool"]

    second = ScriptedAdapter.from_tool_plan(plan[2:])
    runner2, ctx2, events2, _ = _setup(tmp_path, second, max_steps=10)
    out2 = _run(runner2, ctx2, resume=True)
    assert out2.status == "submitted" and out2.steps == 3
    assert out2.usage.input_tokens == 300
    # resumed with the saved history plus one note that the run had stopped
    resumed = second.calls[0]["messages"]
    assert len(resumed) == 6 and resumed[-1]["role"] == "user"
    assert "budget_exceeded" in resumed[-1]["text"]
    assert events2.of_type("run_started")[0].data["resumed"] is True

    third = ScriptedAdapter.from_tool_plan(["should not be called"])
    runner3, ctx3, _, _ = _setup(tmp_path, third)
    assert _run(runner3, ctx3, resume=True).status == "submitted" and third.calls == []


def test_tool_calls_of_a_cut_off_turn_are_not_run(tmp_path):
    truncated = ModelResponse(text="", stop_reason="max_tokens", usage=Usage(1, 1), model="m", tool_calls=[
        ToolCall("c1", "write_file", {"path": REPORT, "content": "# Report\n\nSection 1 ... (cut mid-sent"})])
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    submit = ModelResponse(text="", stop_reason="tool_use", usage=Usage(1, 1), model="m", tool_calls=[
        ToolCall("c2", "submit_milestone", {"summary": "s", "artifacts": ["a.md"]})])
    adapter = ScriptedAdapter([truncated, submit])
    runner, ctx, events, _ = _setup(tmp_path, adapter)
    assert _run(runner, ctx).status == "submitted"
    assert not (tmp_path / REPORT).exists()
    answer = adapter.calls[1]["messages"][-1]
    assert answer["role"] == "tool" and answer["tool_results"][0]["call_id"] == "c1"
    assert answer["tool_results"][0]["is_error"] and "Not run" in answer["tool_results"][0]["content"]
    assert [e.data["name"] for e in events.of_type("tool_call")] == ["submit_milestone"]
    assert [n.data["reason"] for n in events.of_type("nudge")] == ["cut_off"]


def test_resuming_a_stopped_run_never_ends_on_an_assistant_turn(tmp_path):
    first = ScriptedAdapter.from_tool_plan(["I think I'm done.", "Still done."])
    runner, ctx, _, journal = _setup(tmp_path, first)
    assert _run(runner, ctx).status == "no_submission"
    assert journal.load_checkpoint("m1").messages[-1].role == "assistant"
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    second = ScriptedAdapter.from_tool_plan([("submit_milestone", {"summary": "s", "artifacts": ["a.md"]})])
    runner2, ctx2, events2, _ = _setup(tmp_path, second)
    assert _run(runner2, ctx2, resume=True).status == "submitted"
    sent = second.calls[0]["messages"]
    assert sent[-1]["role"] == "user" and "no_submission" in sent[-1]["text"]
    assert events2.of_type("nudge")[0].data["reason"] == "resume"


def test_resume_delivers_answers_that_arrived_since_the_checkpoint(tmp_path):
    from agentkit.types import Brief
    runner, ctx, _, journal = _setup(tmp_path, ScriptedAdapter.from_tool_plan([("list_files", {})]),
                                     max_steps=1)
    ctx.brief = Brief("e1", "demo", "obj", answers={"Old?": "known"})
    _run(runner, ctx)
    assert journal.load_checkpoint("m1").answered == ["Old?"]
    adapter = ScriptedAdapter.from_tool_plan(["done", "done"])
    runner2, ctx2, _, journal2 = _setup(tmp_path, adapter)
    ctx2.brief = Brief("e1", "demo", "obj", answers={"Old?": "known", "Which region?": "EMEA only"})
    _run(runner2, ctx2, resume=True)
    note = adapter.calls[0]["messages"][-1]["text"]
    assert "EMEA only" in note and "known" not in note and '<untrusted source="client-answers">' in note
    assert journal2.load_checkpoint("m1").answered == ["Old?", "Which region?"]


def test_resuming_a_refused_run_discards_the_refused_turn(tmp_path):
    refuse = ModelResponse(text="Let me start by", tool_calls=[], stop_reason="refusal", usage=Usage(),
                           model="m")
    first = ScriptedAdapter([ScriptedAdapter.from_tool_plan([("list_files", {})])._steps[0], refuse])
    runner, ctx, _, journal = _setup(tmp_path, first)
    assert _run(runner, ctx).status == "refused"
    assert journal.load_checkpoint("m1").messages[-1].text == "Let me start by"
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    second = ScriptedAdapter.from_tool_plan([("submit_milestone", {"summary": "s", "artifacts": ["a.md"]})])
    runner2, ctx2, events2, _ = _setup(tmp_path, second)
    assert _run(runner2, ctx2, resume=True).status == "submitted"
    sent = second.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "user"]
    assert "refused" in sent[-1]["text"] and all("Let me start by" not in m["text"] for m in sent)
    assert events2.of_type("nudge")[0].data["discarded_refusal"] is True


def test_a_tool_that_stops_the_run_still_leaves_every_call_answered(tmp_path):
    from agentkit.tools import Tool

    def metered(args, ctx):
        raise BudgetExceeded("max_usd", "the paid lookup would exceed the budget")

    registry = builtin_registry()
    registry.add(Tool("paid_lookup", "a metered API", {"type": "object", "properties": {}}, metered))
    adapter = ScriptedAdapter.from_tool_plan([[("list_files", {}), ("paid_lookup", {}), ("list_files", {})]])
    runner, ctx, events, journal = _setup(tmp_path, adapter)
    runner.registry = registry
    out = _run(runner, ctx)
    assert out.status == "budget_exceeded" and out.limit == "max_usd"
    last = journal.load_checkpoint("m1").messages[-1]
    assert last.role == "tool" and [r.call_id for r in last.tool_results] == ["call_1", "call_2", "call_3"]
    assert not last.tool_results[0].is_error
    assert all(r.is_error and "Not run" in r.content for r in last.tool_results[1:])
    second = ScriptedAdapter.from_tool_plan(["done", "done"])
    runner2, ctx2, _, _ = _setup(tmp_path, second)
    _run(runner2, ctx2, resume=True)
    assert [m["role"] for m in second.calls[0]["messages"]] == ["user", "assistant", "tool", "user"]


def test_model_error_ends_the_run_cleanly_and_can_resume(tmp_path):
    from agentkit.errors import ModelError

    def outage(messages):
        raise ModelError("provider down", provider="scripted", retryable=True)

    adapter = ScriptedAdapter([ScriptedAdapter.from_tool_plan([("list_files", {})])._steps[0], outage])
    runner, ctx, events, journal = _setup(tmp_path, adapter)
    out = _run(runner, ctx)
    assert out.status == "model_error" and out.steps == 1
    assert events.of_type("model_error")[0].data["message"] == "provider down"
    assert events.types[-1] == "run_finished" and journal.load_checkpoint("m1").status == "model_error"
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    second = ScriptedAdapter.from_tool_plan([("submit_milestone", {"summary": "s", "artifacts": ["a.md"]})])
    runner2, ctx2, _, _ = _setup(tmp_path, second)
    assert _run(runner2, ctx2, resume=True).status == "submitted"
    roles = [m["role"] for m in second.calls[0]["messages"]]
    assert roles == ["user", "assistant", "tool", "user"]


def test_unpriced_model_emits_cost_unknown_once(tmp_path):
    adapter = ScriptedAdapter.from_tool_plan([("list_files", {"path": str(i)}) for i in range(3)])
    runner, ctx, events, _ = _setup(tmp_path, adapter)
    _run(runner, ctx)
    unknown = events.of_type("cost_unknown")
    assert len(unknown) == 1 and unknown[0].data["model"] == "scripted"


def test_token_limit_does_not_count_cache_reads(tmp_path):
    usage = Usage(input_tokens=10, output_tokens=10, cache_read_tokens=100_000)
    adapter = ScriptedAdapter.from_tool_plan([("list_files", {"path": str(i)}) for i in range(4)] +
                                             ["done", "done"], usage_per_step=usage)
    runner, ctx, _, _ = _setup(tmp_path, adapter, max_tokens=1000)
    out = _run(runner, ctx)
    assert out.status == "no_submission" and out.usage.cache_read_tokens == 600_000


def test_without_resume_starts_fresh(tmp_path):
    runner, ctx, _, _ = _setup(tmp_path, ScriptedAdapter.from_tool_plan(["a", "b"]))
    _run(runner, ctx)
    adapter = ScriptedAdapter.from_tool_plan(["c", "d"])
    runner2, ctx2, _, _ = _setup(tmp_path, adapter)
    _run(runner2, ctx2)
    assert len(adapter.calls[0]["messages"]) == 1


def test_models_that_served_are_recorded_and_server_side_switches_journaled(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    first = ModelResponse(text="", tool_calls=[ToolCall(id="c1", name="list_files", arguments={})],
                          stop_reason="tool_use", usage=Usage(1, 1), model="claude-opus-5",
                          raw={"provider": "anthropic", "content": []})
    rescued = ModelResponse(text="", tool_calls=[ToolCall(id="c2", name="submit_milestone", arguments={
        "summary": "ok", "artifacts": ["a.md"]})], stop_reason="tool_use", usage=Usage(1, 1),
        model="claude-opus-4-8", raw={"provider": "anthropic", "content": []},
        fallbacks=[{"from": "anthropic:claude-opus-5", "to": "anthropic:claude-opus-4-8"}])
    runner, ctx, events, journal = _setup(tmp_path, ScriptedAdapter([first, rescued]))
    out = _run(runner, ctx)
    assert out.status == "submitted"
    assert out.models == ["anthropic:claude-opus-5", "anthropic:claude-opus-4-8"]
    (switch,) = events.of_type("model_fallback")
    assert switch.data == {"step": 2, "from_model": "anthropic:claude-opus-5",
                           "to_model": "anthropic:claude-opus-4-8", "reason": "server_side"}
    # the checkpoint keeps them, so a resumed run reports the same models
    assert journal.load_checkpoint("m1").models == out.models
    runner2, ctx2, _, _ = _setup(tmp_path, ScriptedAdapter([]))
    assert _run(runner2, ctx2, resume=True).models == out.models


def test_a_model_without_provider_raw_is_recorded_with_the_adapter_provider(tmp_path):
    adapter = ScriptedAdapter.from_tool_plan(["done", "done"], model="m-1")
    runner, ctx, _, _ = _setup(tmp_path, adapter)
    assert _run(runner, ctx).models == ["scripted:m-1"]
