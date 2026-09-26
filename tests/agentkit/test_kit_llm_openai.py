"""OpenAICompatAdapter against a fake client that returns real openai SDK types."""
import json

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from agentkit.errors import ModelError
from agentkit.llm import ModelRef, get_adapter
from agentkit.llm.openai_compat import OpenAICompatAdapter
from agentkit.types import Message, ToolCall, ToolResult, ToolSpec

TOOLS = [ToolSpec(name="read_file", description="Read a workspace file.",
                  input_schema={"type": "object", "properties": {"path": {"type": "string"}}})]
KEY = "sk-" + "test-" + "0" * 8


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, *responses):
        self.completions = FakeCompletions(responses)
        self.chat = type("Chat", (), {})()
        self.chat.completions = self.completions


def completion(content=None, tool_calls=None, finish_reason="stop", model="gpt-test",
               usage=None, refusal=None):
    message = {"role": "assistant", "content": content, "refusal": refusal}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return ChatCompletion.model_validate({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1_700_000_000,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason,
                     "logprobs": None}],
        "usage": usage or {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    })


def fn_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def adapter(provider="openai", model="gpt-test", client=None, env=None, **options):
    env = {"OPENAI_API_KEY": KEY} if env is None else env
    return OpenAICompatAdapter(ModelRef(provider, model), env=env, client=client, options=options)


# --- request shape ----------------------------------------------------------------

def test_request_uses_function_tools_and_max_completion_tokens_for_openai():
    client = FakeClient(completion("hi"))
    adapter(client=client).complete(system="Be brief.", messages=[Message.user("hello")],
                                    tools=TOOLS, max_tokens=2000)
    assert client.completions.calls[0] == {
        "model": "gpt-test",
        "messages": [{"role": "system", "content": "Be brief."},
                     {"role": "user", "content": "hello"}],
        "max_completion_tokens": 2000,
        "tools": [{"type": "function", "function": {
            "name": "read_file", "description": "Read a workspace file.",
            "parameters": TOOLS[0].input_schema}}],
    }


def test_other_presets_send_max_tokens_and_optional_reasoning_effort():
    a = adapter("gemini", "gemini-test", env={"GEMINI_API_KEY": KEY}, reasoning_effort="low")
    req = a.build_request(system="", messages=[Message.user("x")], tools=[], max_tokens=99)
    assert req == {"model": "gemini-test", "messages": [{"role": "user", "content": "x"}],
                   "max_tokens": 99, "reasoning_effort": "low"}


def test_history_rebuilds_tool_calls_as_json_strings_and_one_tool_message_per_result():
    history = [
        Message.user("go"),
        Message(role="assistant", text="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a"}),
                            ToolCall(id="c2", name="read_file", arguments={"path": "b"})]),
        Message.tool([ToolResult(call_id="c1", name="read_file", content="A"),
                      ToolResult(call_id="c2", name="read_file", content="missing",
                                 is_error=True)]),
    ]
    wire = adapter().to_wire(history)
    assert wire[1] == {"role": "assistant", "content": None, "tool_calls": [
        fn_call("c1", "read_file", json.dumps({"path": "a"})),
        fn_call("c2", "read_file", json.dumps({"path": "b"}))]}
    assert wire[2:] == [{"role": "tool", "tool_call_id": "c1", "content": "A"},
                        {"role": "tool", "tool_call_id": "c2", "content": "Error: missing"}]


def test_same_provider_and_model_replays_raw_message_with_provider_extras():
    call = fn_call("c1", "read_file", '{"path": "a"}')
    call["extra_content"] = {"google": {"thought_signature": "sig"}}
    resp = completion(None, tool_calls=[call], finish_reason="tool_calls", model="gemini-test")
    a = adapter("gemini", "gemini-test", env={"GEMINI_API_KEY": KEY})
    turn = a.parse_response(resp).to_message()
    answered = Message.tool([ToolResult(call_id="c1", name="read_file", content="A")])
    wire = a.to_wire([turn, answered])
    assert wire[0] == {"role": "assistant", "tool_calls": [call]}
    # a different model gets the neutral rebuild instead
    other = adapter("gemini", "gemini-other", env={"GEMINI_API_KEY": KEY}).to_wire([turn, answered])
    assert "extra_content" not in other[0]["tool_calls"][0]


def test_unanswered_tool_calls_are_not_replayed():
    # e.g. a refused turn's partial calls, replayed when the run is resumed
    raw_turn = adapter().parse_response(completion("partial", finish_reason="tool_calls", tool_calls=[
        fn_call("c1", "read_file", '{"path": "a"}'), fn_call("c2", "read_file", '{"path": "b"}')])).to_message()
    history = [Message.user("go"), raw_turn,
               Message.tool([ToolResult(call_id="c1", name="read_file", content="A")]), Message.user("resume")]
    wire = adapter().to_wire(history)
    assert [c["id"] for c in wire[1]["tool_calls"]] == ["c1"]
    lone = adapter().to_wire([Message.user("go"), Message(role="assistant", tool_calls=[
        ToolCall("c9", "read_file", {"path": "x"})]), Message.user("resume")])
    assert [m["role"] for m in lone] == ["user", "user"]
    # a refusal with partial calls keeps its refusal text, loses the calls
    refused = adapter().parse_response(completion(None, finish_reason="tool_calls", refusal="I can't",
                                                  tool_calls=[fn_call("c3", "read_file", "{}")])).to_message()
    wire = adapter().to_wire([Message.user("go"), refused, Message.user("resume")])
    assert wire[1] == {"role": "assistant", "refusal": "I can't"}


# --- response parsing --------------------------------------------------------------

def test_tool_calls_response_parses_arguments_and_usage():
    resp = completion("Let me look.", finish_reason="tool_calls",
                      tool_calls=[fn_call("c1", "read_file", '{"path": "a.md"}'),
                                  fn_call("c2", "list_files", "")],
                      usage={"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                             "prompt_tokens_details": {"cached_tokens": 600}})
    out = adapter(client=FakeClient(resp)).complete(system="", messages=[Message.user("x")],
                                                    tools=TOOLS)
    assert out.stop_reason == "tool_use" and out.text == "Let me look."
    assert out.tool_calls == [ToolCall(id="c1", name="read_file", arguments={"path": "a.md"}),
                              ToolCall(id="c2", name="list_files", arguments={})]
    assert (out.usage.input_tokens, out.usage.cache_read_tokens, out.usage.output_tokens) == \
        (400, 600, 50)
    assert out.usage.cost_usd is None  # no price known for gpt-test
    assert out.raw["provider"] == "openai" and out.raw["model"] == "gpt-test"


@pytest.mark.parametrize("arguments,fragment", [
    ('{"path": "a"', "not valid JSON"),
    ('["a", "b"]', "must be a JSON object"),
])
def test_invalid_tool_arguments_become_invalid_calls(arguments, fragment):
    resp = completion(None, finish_reason="tool_calls",
                      tool_calls=[fn_call("c1", "read_file", arguments)])
    call = adapter().parse_response(resp).tool_calls[0]
    assert call.name == "read_file" and call.arguments == {}
    assert fragment in call.invalid


@pytest.mark.parametrize("finish,neutral", [
    ("stop", "end"), ("length", "max_tokens"), ("tool_calls", "tool_use"),
    ("content_filter", "refusal"),
])
def test_finish_reasons_are_mapped(finish, neutral):
    assert adapter().parse_response(completion("x", finish_reason=finish)).stop_reason == neutral


def test_tool_calls_with_stop_finish_reason_still_mean_tool_use():
    resp = completion(None, tool_calls=[fn_call("c1", "read_file", "{}")], finish_reason="stop")
    assert adapter().parse_response(resp).stop_reason == "tool_use"


def test_refusal_field_maps_to_refusal():
    out = adapter().parse_response(completion(None, refusal="I can't help with that."))
    assert out.stop_reason == "refusal" and out.text == "I can't help with that."


def test_priced_model_gets_cost_from_pricing_file(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("openai:gpt-test: {input: 2, output: 8}\n", encoding="utf-8")
    a = adapter(env={"OPENAI_API_KEY": KEY, "AGENTKIT_PRICING_FILE": str(f)})
    out = a.parse_response(completion("x", usage={"prompt_tokens": 1_000_000,
                                                  "completion_tokens": 1_000_000,
                                                  "total_tokens": 2_000_000}))
    assert out.usage.cost_usd == pytest.approx(10.0)


# --- presets, errors, construction ------------------------------------------------------

def test_preset_client_kwargs():
    assert adapter().client_kwargs() == {"api_key": KEY}
    assert adapter("gemini", env={"GEMINI_API_KEY": KEY}).client_kwargs() == {
        "api_key": KEY, "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"}
    assert adapter("openrouter", env={"OPENROUTER_API_KEY": KEY}).client_kwargs()["base_url"] == \
        "https://openrouter.ai/api/v1"
    assert adapter("ollama", env={}).client_kwargs() == {
        "api_key": "not-needed", "base_url": "http://localhost:11434/v1"}
    assert adapter("ollama", env={"OLLAMA_BASE_URL": "http://gpu:11434/v1"}).client_kwargs()[
        "base_url"] == "http://gpu:11434/v1"
    compat_env = {"AGENTKIT_OPENAI_COMPAT_BASE_URL": "http://vllm:8000/v1",
                  "AGENTKIT_OPENAI_COMPAT_API_KEY": KEY}
    for provider in ("vllm", "openai_compat"):
        assert adapter(provider, env=compat_env, max_retries=1).client_kwargs() == {
            "api_key": KEY, "base_url": "http://vllm:8000/v1", "max_retries": 1}


def test_missing_key_or_base_url_is_a_model_error():
    with pytest.raises(ModelError, match="OPENAI_API_KEY"):
        adapter(env={}).client_kwargs()
    with pytest.raises(ModelError, match="AGENTKIT_OPENAI_COMPAT_BASE_URL"):
        adapter("vllm", env={}).client_kwargs()


def test_client_is_built_lazily_from_preset():
    a = adapter("openrouter", env={"OPENROUTER_API_KEY": KEY})
    assert a._client is None
    assert a.client.api_key == KEY and str(a.client.base_url).startswith("https://openrouter.ai")


def _http_error(cls, status):
    req = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return cls("boom", response=httpx2.Response(status, request=req), body=None)


@pytest.mark.parametrize("exc,retryable", [
    (_http_error(openai.RateLimitError, 429), True),
    (_http_error(openai.InternalServerError, 503), True),
    (_http_error(openai.BadRequestError, 400), False),
    (openai.APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com")), True),
])
def test_sdk_errors_become_model_errors(exc, retryable):
    with pytest.raises(ModelError) as err:
        adapter(client=FakeClient(exc)).complete(system="", messages=[Message.user("x")],
                                                 tools=[])
    assert err.value.retryable is retryable and err.value.provider == "openai"


def test_get_adapter_routes_every_compat_provider():
    for provider in ("openai", "gemini", "openrouter", "ollama", "vllm", "openai_compat"):
        a = get_adapter(f"{provider}:m", client=FakeClient())
        assert isinstance(a, OpenAICompatAdapter) and a.ref.provider == provider
