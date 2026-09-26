"""AnthropicAdapter against a fake client that returns real anthropic SDK types."""
import anthropic
import httpx2
import pytest
from anthropic.types import Message as SDKMessage
from anthropic.types.beta import BetaMessage

from agentkit.errors import ModelError
from agentkit.llm import ModelRef, get_adapter
from agentkit.llm.anthropic import AnthropicAdapter
from agentkit.types import Message, ToolCall, ToolResult, ToolSpec

TOOLS = [ToolSpec(name="read_file", description="Read a workspace file.",
                  input_schema={"type": "object", "properties": {"path": {"type": "string"}},
                                "required": ["path"]})]
NO_ENV: dict = {}


class _Endpoint:
    def __init__(self, name, client):
        self.name, self.client = name, client

    def create(self, **kwargs):
        self.client.calls.append((self.name, kwargs))
        item = self.client.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    """Duck-types client.messages.create and client.beta.messages.create."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = _Endpoint("messages", self)
        self.beta = type("Beta", (), {})()
        self.beta.messages = _Endpoint("beta.messages", self)


def sdk_message(content, stop_reason="end_turn", model="claude-opus-5", usage=None, beta=False,
                **extra):
    data = {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop_reason,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 200}, **extra}
    return (BetaMessage if beta else SDKMessage).model_validate(data)


def adapter(model="claude-opus-5", client=None, **options):
    return AnthropicAdapter(ModelRef("anthropic", model), env=NO_ENV, client=client,
                            options=options)


def _http_error(cls, status):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=req), body=None)


# --- request shape ----------------------------------------------------------------

def test_opus_5_request_uses_adaptive_thinking_and_cache_and_no_server_fallbacks():
    # By default a run stays on the stamped model: no server-side fallbacks.
    client = FakeClient(sdk_message([{"type": "text", "text": "ok"}]))
    a = adapter(client=client)
    a.complete(system="You are careful.", messages=[Message.user("hi")], tools=TOOLS,
               max_tokens=4000)
    endpoint, kwargs = client.calls[0]
    assert endpoint == "messages"
    assert kwargs == {
        "model": "claude-opus-5",
        "max_tokens": 4000,
        "system": "You are careful.",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "cache_control": {"type": "ephemeral"},
        "tools": [{"name": "read_file", "description": "Read a workspace file.",
                   "input_schema": TOOLS[0].input_schema}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    assert "tool_choice" not in kwargs


def test_server_fallbacks_are_an_explicit_opt_in():
    client = FakeClient(sdk_message([{"type": "text", "text": "ok"}], beta=True))
    adapter(client=client, server_fallbacks=True, effort="medium").complete(
        system="", messages=[Message.user("hi")], tools=[])
    endpoint, kwargs = client.calls[0]
    assert endpoint == "beta.messages"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"] and kwargs["fallbacks"] == "default"
    assert "system" not in kwargs and "tools" not in kwargs
    assert kwargs["output_config"] == {"effort": "medium"}
    # anything but a literal true (a typo, a string) leaves them off
    for value in (None, False, "true", 1):
        assert not adapter(server_fallbacks=value).uses_server_fallbacks()
    assert "fallbacks" not in get_adapter("anthropic:claude-opus-5", env=NO_ENV).build_request(
        system="s", messages=[Message.user("x")], tools=[])


def test_fable_5_1_can_opt_into_server_fallbacks_but_sonnet_5_cannot():
    assert adapter("claude-fable-5-1", server_fallbacks=True).uses_server_fallbacks()
    assert adapter("claude-opus-5-5", server_fallbacks=True).uses_server_fallbacks()
    assert adapter("claude-mythos-5-1", server_fallbacks=True).uses_server_fallbacks()
    assert not adapter("claude-opus-5-5").uses_server_fallbacks()
    assert not adapter("claude-sonnet-5", server_fallbacks=True).uses_server_fallbacks()
    req = adapter("claude-sonnet-5").build_request(system="s", messages=[Message.user("x")],
                                                   tools=[])
    assert req["thinking"] == {"type": "adaptive"} and "fallbacks" not in req


def test_haiku_4_5_gets_no_thinking_or_effort():
    req = adapter("claude-haiku-4-5").build_request(system="s", messages=[Message.user("x")],
                                                    tools=[])
    assert "thinking" not in req and "output_config" not in req and "betas" not in req


# --- response parsing --------------------------------------------------------------

def test_tool_use_response_maps_calls_usage_cost_and_keeps_raw_thinking():
    resp = sdk_message(
        [{"type": "thinking", "thinking": "", "signature": "sig-abc"},
         {"type": "text", "text": "Reading it."},
         {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.md"}}],
        stop_reason="tool_use",
        usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000,
               "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000})
    out = adapter(client=FakeClient(resp), server_fallbacks=False).complete(
        system="s", messages=[Message.user("go")], tools=TOOLS)
    assert out.stop_reason == "tool_use"
    assert out.text == "Reading it."
    assert out.tool_calls == [ToolCall(id="toolu_1", name="read_file", arguments={"path": "a.md"})]
    assert (out.usage.input_tokens, out.usage.output_tokens,
            out.usage.cache_read_tokens, out.usage.cache_write_tokens) == (10**6,) * 4
    assert out.usage.cost_usd == pytest.approx(5 + 25 + 0.5 + 6.25)
    assert out.raw["provider"] == "anthropic" and out.raw["model"] == "claude-opus-5"
    assert out.raw["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}


@pytest.mark.parametrize("sdk_reason,neutral", [
    ("end_turn", "end"), ("stop_sequence", "end"), ("tool_use", "tool_use"),
    ("max_tokens", "max_tokens"), ("model_context_window_exceeded", "max_tokens"),
    ("refusal", "refusal"), ("pause_turn", "pause"),
])
def test_stop_reasons_are_mapped(sdk_reason, neutral):
    resp = sdk_message([{"type": "text", "text": "x"}], stop_reason=sdk_reason)
    assert adapter(server_fallbacks=False).parse_response(resp).stop_reason == neutral


def test_refusal_drops_partial_tool_calls_and_keeps_stop_details():
    resp = sdk_message(
        [{"type": "tool_use", "id": "toolu_9", "name": "read_file", "input": {"path": "x"}}],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": None})
    out = adapter().parse_response(resp)
    assert out.stop_reason == "refusal" and out.tool_calls == []
    assert out.raw["stop_details"]["category"] == "cyber"


def test_unknown_model_has_no_cost():
    resp = sdk_message([{"type": "text", "text": "x"}], model="claude-imaginary-9")
    out = adapter("claude-imaginary-9").parse_response(resp)
    assert out.usage.input_tokens == 1000 and out.usage.cost_usd is None


def test_sticky_fallback_turns_count_toward_the_usd_limit():
    # Once a conversation falls back, later turns are served by claude-opus-4-8
    # directly; they must be priced, or max_usd silently stops binding.
    sticky = sdk_message([{"type": "text", "text": "x"}], model="claude-opus-4-8", beta=True,
                         usage={"input_tokens": 1_000_000, "output_tokens": 0, "iterations": [
                             {"type": "fallback_message", "model": "claude-opus-4-8",
                              "input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]})
    assert adapter().parse_response(sticky).usage.cost_usd == pytest.approx(5.0)


def test_server_fallback_response_uses_serving_model_and_iteration_usage():
    resp = sdk_message(
        [{"type": "fallback", "from": {"model": "claude-opus-5"},
          "to": {"model": "claude-opus-4-8"}, "trigger": {"type": "refusal", "category": "cyber"}},
         {"type": "text", "text": "Here is the review."}],
        model="claude-opus-4-8", beta=True,
        usage={"input_tokens": 50, "output_tokens": 60, "iterations": [
            {"type": "message", "model": "claude-opus-5", "input_tokens": 1_000_000,
             "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 50,
             "output_tokens": 60, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}]})
    out = adapter().parse_response(resp)
    assert out.model == "claude-opus-4-8" and out.stop_reason == "end"
    assert out.text == "Here is the review."
    assert out.usage.input_tokens == 1_000_050 and out.usage.output_tokens == 60
    # the declined attempt at Opus 5 rates plus the fallback at Opus 4.8 rates
    assert out.usage.cost_usd == pytest.approx(5.0 + (50 * 5 + 60 * 25) / 1e6)
    assert out.raw["content"][0]["from"] == {"model": "claude-opus-5"}
    assert out.fallbacks == [{"from": "anthropic:claude-opus-5", "to": "anthropic:claude-opus-4-8"}]


def test_sticky_fallback_turn_reports_the_switch_without_a_block():
    sticky = sdk_message([{"type": "text", "text": "x"}], model="claude-opus-4-8", beta=True,
                         usage={"input_tokens": 10, "output_tokens": 0, "iterations": [
                             {"type": "fallback_message", "model": "claude-opus-4-8",
                              "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}]})
    assert adapter().parse_response(sticky).fallbacks == [
        {"from": "anthropic:claude-opus-5", "to": "anthropic:claude-opus-4-8"}]
    plain = sdk_message([{"type": "text", "text": "x"}])
    assert adapter().parse_response(plain).fallbacks == []


def test_iteration_without_a_model_is_priced_at_the_model_that_ran_it():
    resp = sdk_message(
        [{"type": "text", "text": "ok"}], model="claude-imaginary-9", beta=True,
        usage={"input_tokens": 0, "output_tokens": 0, "iterations": [
            {"type": "message", "input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-imaginary-9", "input_tokens": 1_000_000,
             "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]})
    # the model-less first entry ran on the requested model (priced at Opus 5
    # rates, not at the unpriced serving model's); the second is unpriced
    assert adapter().parse_response(resp).usage.cost_usd == pytest.approx(5.0)


# --- history replay ------------------------------------------------------------------

def _raw_turn(model="claude-opus-5"):
    return Message(
        role="assistant", text="Reading both.",
        tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "a"}),
                    ToolCall(id="t2", name="read_file", arguments={"path": "b"})],
        raw={"provider": "anthropic", "model": model, "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "text", "text": "Reading both."},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}},
            {"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "b"}}]})


def test_same_model_replays_raw_content_and_batches_tool_results():
    history = [
        Message.user("go"),
        _raw_turn(),
        Message.tool([ToolResult(call_id="t1", name="read_file", content="A"),
                      ToolResult(call_id="t2", name="read_file", content="no such file",
                                 is_error=True)]),
        Message.user("keep going"),
    ]
    wire = adapter().to_wire(history)
    assert wire[1] == {"role": "assistant", "content": history[1].raw["content"]}
    assert wire[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "A"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "no such file", "is_error": True},
        {"type": "text", "text": "keep going"}]}
    assert len(wire) == 3


def test_other_claude_model_replays_raw_content_other_provider_rebuilds():
    results = Message.tool([ToolResult(call_id="t1", name="read_file", content="A"),
                            ToolResult(call_id="t2", name="read_file", content="B")])
    # another Claude model (a fallback, or --model on resume): thinking kept unchanged
    turn = _raw_turn(model="claude-sonnet-5")
    wire = adapter().to_wire([Message.user("go"), turn, results])
    assert wire[1]["content"] == turn.raw["content"]
    # another provider: rebuilt from text + tool_calls
    other = Message(role="assistant", text="Reading both.", tool_calls=_raw_turn().tool_calls,
                    raw={"provider": "openai", "model": "claude-opus-5", "content": []})
    wire = adapter().to_wire([Message.user("go"), other, results])
    assert wire[1]["content"] == [
        {"type": "text", "text": "Reading both."},
        {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}},
        {"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "b"}}]


def test_replay_drops_fallback_marker_and_declined_partial():
    turn = Message(role="assistant", raw={"provider": "anthropic", "model": "claude-opus-5",
                                          "content": [
        {"type": "thinking", "thinking": "", "signature": "declined"},
        {"type": "text", "text": "Partial "},
        {"type": "fallback", "from": {"model": "claude-opus-5"},
         "to": {"model": "claude-opus-4-8"}, "trigger": {"type": "refusal"}},
        {"type": "thinking", "thinking": "", "signature": "served"},
        {"type": "text", "text": "answer"}]})
    content = adapter().to_wire([Message.user("q"), turn])[1]["content"]
    assert [b["type"] for b in content] == ["text", "thinking", "text"]
    assert content[1]["signature"] == "served"


def test_empty_assistant_turn_is_skipped():
    wire = adapter().to_wire([Message.user("a"), Message(role="assistant"), Message.user("b")])
    assert wire == [{"role": "user", "content": [{"type": "text", "text": "a"},
                                                 {"type": "text", "text": "b"}]}]


# --- through the Runner ------------------------------------------------------------------

def _runner(tmp_path, client, *, model="claude-opus-5"):
    from agentkit.journal import Journal
    from agentkit.loop import Runner
    from agentkit.policy import PolicyGate
    from agentkit.tools import ToolContext, builtin_registry

    ctx = ToolContext(workspace=tmp_path, policy=PolicyGate())
    runner = Runner(adapter(model, client=client), builtin_registry(), journal=Journal.for_workspace(tmp_path))
    return runner, ctx


def _run(runner, ctx, **kw):
    return runner.run(ctx, system="sys", task="Write the report.", milestone_id="m1", **kw)


def test_truncated_tool_use_is_answered_not_run(tmp_path):
    cut = sdk_message([{"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {
        "path": "deliverables/report.md", "content": "# Report\n\nSection 1 ... (cut mid-sent"}}],
        stop_reason="max_tokens", beta=True)
    client = FakeClient(cut, sdk_message([{"type": "text", "text": "done"}], beta=True),
                        sdk_message([{"type": "text", "text": "done"}], beta=True))
    runner, ctx = _runner(tmp_path, client)
    assert _run(runner, ctx).status == "no_submission"
    assert not (tmp_path / "deliverables" / "report.md").exists()
    second = client.calls[1][1]["messages"]
    assert second[-1]["role"] == "user"
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1" and result["is_error"]


def test_resumed_stopped_run_ends_with_a_user_turn_on_the_wire(tmp_path):
    first = FakeClient(sdk_message([{"type": "text", "text": "I think I'm done."}], beta=True),
                       sdk_message([{"type": "text", "text": "Still done."}], beta=True))
    runner, ctx = _runner(tmp_path, first)
    assert _run(runner, ctx).status == "no_submission"
    second = FakeClient(sdk_message([{"type": "text", "text": "ok"}], beta=True),
                        sdk_message([{"type": "text", "text": "ok"}], beta=True))
    runner, ctx = _runner(tmp_path, second)
    _run(runner, ctx, resume=True)
    wire = second.calls[0][1]["messages"]
    assert [m["role"] for m in wire] == ["user", "assistant", "user", "assistant", "user"]


def test_resumed_refusal_does_not_replay_the_declined_partial(tmp_path):
    refused = sdk_message(
        [{"type": "text", "text": "Let me "},
         {"type": "tool_use", "id": "toolu_9", "name": "read_file", "input": {"path": "x"}}],
        stop_reason="refusal", beta=True,
        stop_details={"type": "refusal", "category": "cyber", "explanation": None})
    runner, ctx = _runner(tmp_path, FakeClient(refused))
    assert _run(runner, ctx).status == "refused"
    second = FakeClient(sdk_message([{"type": "text", "text": "ok"}], beta=True),
                        sdk_message([{"type": "text", "text": "ok"}], beta=True))
    runner, ctx = _runner(tmp_path, second)
    _run(runner, ctx, resume=True)
    wire = second.calls[0][1]["messages"]
    assert [m["role"] for m in wire] == ["user"]            # task + resume note, one user turn
    assert not [b for m in wire for b in m["content"] if b.get("type") == "tool_use"]
    assert "Let me " not in [b.get("text") for m in wire for b in m["content"]]


def test_unanswered_tool_use_is_still_left_out_of_a_replayed_turn():
    turn = Message(role="assistant", text="",
                   tool_calls=[ToolCall("toolu_9", "read_file", {"path": "x"})],
                   raw={"provider": "anthropic", "model": "claude-opus-5", "content": [
                       {"type": "text", "text": "Reading."},
                       {"type": "tool_use", "id": "toolu_9", "name": "read_file", "input": {"path": "x"}}]})
    wire = adapter().to_wire([Message.user("go"), turn, Message.user("continue")])
    assert wire[1]["content"] == [{"type": "text", "text": "Reading."}]


# --- errors and construction ----------------------------------------------------------

@pytest.mark.parametrize("exc,retryable", [
    (_http_error(anthropic.RateLimitError, 429), True),
    (_http_error(anthropic.InternalServerError, 500), True),
    (_http_error(anthropic.BadRequestError, 400), False),
    (_http_error(anthropic.AuthenticationError, 401), False),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), True),
])
def test_sdk_errors_become_model_errors(exc, retryable):
    a = adapter(client=FakeClient(exc))
    with pytest.raises(ModelError) as err:
        a.complete(system="s", messages=[Message.user("x")], tools=[])
    assert err.value.retryable is retryable and err.value.provider == "anthropic"


def test_get_adapter_builds_anthropic_adapter_with_injected_client():
    client = FakeClient()
    a = get_adapter("anthropic:claude-sonnet-5", client=client, options={"effort": "low"})
    assert isinstance(a, AnthropicAdapter) and a.client is client
    assert a.build_request(system="", messages=[], tools=[])["output_config"] == {"effort": "low"}


def test_client_is_built_lazily_from_env_key():
    key = "sk-ant-" + "test-" + "0" * 8
    a = AnthropicAdapter(ModelRef("anthropic", "claude-opus-5"),
                         env={"ANTHROPIC_API_KEY": key}, options={"max_retries": 0})
    assert a._client is None
    assert a.client.api_key == key and a.client.max_retries == 0
