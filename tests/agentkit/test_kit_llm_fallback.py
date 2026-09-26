"""FallbackAdapter moves down the chain on errors and refusals; build_chain wiring."""
import pytest

from agentkit.errors import ModelError
from agentkit.llm import FallbackAdapter, ModelRef, ScriptedAdapter, build_chain
from agentkit.llm.anthropic import AnthropicAdapter
from agentkit.llm.openai_compat import OpenAICompatAdapter
from agentkit.types import Message, ModelResponse, Usage

KEY = "sk-" + "test-" + "0" * 8
ASK = {"system": "s", "messages": [Message.user("hi")], "tools": []}


def answer(text, stop="end", tokens=10):
    return ModelResponse(text=text, tool_calls=[], stop_reason=stop,
                         usage=Usage(input_tokens=tokens, output_tokens=tokens, cost_usd=0.01),
                         model="m")


class Failing:
    def __init__(self, name, *, retryable):
        self.ref = ModelRef("scripted", name)
        self.retryable = retryable
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        raise ModelError(f"{self.ref.model} down", provider="scripted", retryable=self.retryable)


def test_refusal_moves_to_next_model_sticks_and_counts_usage():
    first = ScriptedAdapter([answer("", stop="refusal", tokens=5)], model="a")
    second = ScriptedAdapter([answer("done"), answer("again")], model="b")
    switches = []
    chain = FallbackAdapter([first, second], on_switch=lambda f, t, r: switches.append((f, t, r)))
    out = chain.complete(**ASK)
    assert out.text == "done" and out.stop_reason == "end"
    assert out.usage.input_tokens == 15 and out.usage.cost_usd == pytest.approx(0.02)
    assert chain.ref == ModelRef("scripted", "b")
    assert switches == [(ModelRef("scripted", "a"), ModelRef("scripted", "b"), "refusal")]
    # sticky: the refusing model is not asked again
    assert chain.complete(**ASK).text == "again"
    assert len(first.calls) == 1 and len(second.calls) == 2


def test_retryable_error_skips_only_this_call():
    flaky = Failing("a", retryable=True)
    backup = ScriptedAdapter([answer("one"), answer("two")], model="b")
    chain = FallbackAdapter([flaky, backup])
    assert chain.complete(**ASK).text == "one"
    assert chain.complete(**ASK).text == "two"
    assert flaky.calls == 2  # the primary is tried again on the next call


def test_non_retryable_error_is_sticky():
    broken = Failing("a", retryable=False)
    backup = ScriptedAdapter([answer("one"), answer("two")], model="b")
    chain = FallbackAdapter([broken, backup])
    chain.complete(**ASK)
    chain.complete(**ASK)
    assert broken.calls == 1


def test_all_errors_raise_one_model_error():
    chain = FallbackAdapter([Failing("a", retryable=False), Failing("b", retryable=True)])
    with pytest.raises(ModelError, match="a down; b down") as err:
        chain.complete(**ASK)
    assert err.value.retryable is True


def test_last_refusal_is_returned_when_nothing_else_answers():
    only_refusals = FallbackAdapter([ScriptedAdapter([answer("", stop="refusal")], model="a"),
                                     ScriptedAdapter([answer("no", stop="refusal")], model="b")])
    out = only_refusals.complete(**ASK)
    assert out.stop_reason == "refusal" and out.usage.input_tokens == 20

    refusal_then_error = FallbackAdapter([ScriptedAdapter([answer("", stop="refusal")], model="a"),
                                          Failing("b", retryable=True)])
    out = refusal_then_error.complete(**ASK)
    assert out.stop_reason == "refusal" and out.usage.input_tokens == 10


def test_empty_chain_is_rejected():
    with pytest.raises(ValueError):
        FallbackAdapter([])


def test_build_chain_wires_providers_clients_and_options():
    env = {"OPENAI_API_KEY": KEY, "GEMINI_API_KEY": KEY}
    fake = object()
    chain = build_chain("anthropic:claude-opus-5", "openai:gpt-test, gemini:gem-test", env=env,
                        clients={"openai:gpt-test": fake},
                        options={"anthropic": {"effort": "xhigh"}})
    assert isinstance(chain, FallbackAdapter)
    a, o, g = chain.adapters
    assert isinstance(a, AnthropicAdapter) and a.options == {"effort": "xhigh"}
    assert isinstance(o, OpenAICompatAdapter) and o.client is fake and o.options == {}
    assert g.ref == ModelRef("gemini", "gem-test")


def test_build_chain_ignores_env_fallbacks_without_the_override_switch():
    env = {"AGENTKIT_FALLBACK_MODELS": "openai:gpt-x", "OPENAI_API_KEY": KEY}
    assert isinstance(build_chain("anthropic:claude-opus-5", env=env), AnthropicAdapter)
    for value in ("0", "true", ""):
        chain = build_chain("anthropic:claude-opus-5",
                            env={**env, "AGENTKIT_ALLOW_MODEL_OVERRIDE": value})
        assert isinstance(chain, AnthropicAdapter), value
    # explicit fallbacks (the manifest's) are always used
    chain = build_chain("anthropic:claude-opus-5", ["openai:gpt-y"], env=env)
    assert [str(x.ref) for x in chain.adapters] == ["anthropic:claude-opus-5", "openai:gpt-y"]


def test_build_chain_reads_env_list_dedupes_and_unwraps_single():
    env = {"AGENTKIT_FALLBACK_MODELS": "anthropic:claude-opus-5,openai:gpt-test",
           "OPENAI_API_KEY": KEY, "AGENTKIT_ALLOW_MODEL_OVERRIDE": "1"}
    chain = build_chain("anthropic:claude-opus-5", env=env)
    assert [str(x.ref) for x in chain.adapters] == ["anthropic:claude-opus-5", "openai:gpt-test"]
    single = build_chain("anthropic:claude-sonnet-5", [], env=env)
    assert isinstance(single, AnthropicAdapter)
    with pytest.raises(ValueError):
        build_chain("anthropic:claude-opus-5", "nonsense", env={})
