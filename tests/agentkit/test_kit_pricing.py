"""Token pricing: built-in Claude table, YAML overrides, unknown models."""
import pytest

from agentkit.pricing import BUILTIN_PRICES, Price, cost_usd, load_pricing_file, price_for
from agentkit.types import Usage

NO_ENV: dict = {}


def test_builtin_prices_match_reference_table():
    assert price_for("anthropic", "claude-opus-5", env=NO_ENV) == Price(5.0, 25.0)
    assert price_for("anthropic", "claude-sonnet-5", env=NO_ENV) == Price(2.0, 10.0)
    assert price_for("anthropic", "claude-haiku-4-5", env=NO_ENV) == Price(1.0, 5.0)
    assert price_for("anthropic", "claude-opus-5-5", env=NO_ENV).input == 4.0
    assert price_for("anthropic", "claude-fable-5-1", env=NO_ENV).output == 50.0
    # server-side fallback targets and earlier models still served
    assert price_for("anthropic", "claude-opus-4-8", env=NO_ENV) == Price(5.0, 25.0)
    assert price_for("anthropic", "claude-sonnet-4-6", env=NO_ENV) == Price(3.0, 15.0)
    assert price_for("anthropic", "claude-mythos-5-1", env=NO_ENV).cache_read_rate == 0.25
    assert price_for("anthropic", "claude-fable-5", env=NO_ENV).cache_read_rate == pytest.approx(1.0)
    assert all(key.startswith("anthropic:") for key in BUILTIN_PRICES)


def test_cost_includes_cache_multipliers():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000,
                  cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    # 5 input + 25 output + 0.5 cache read (0.1x) + 6.25 cache write (1.25x)
    assert cost_usd("anthropic", "claude-opus-5", usage, env=NO_ENV) == pytest.approx(36.75)


def test_explicit_cache_read_rate_wins_over_multiplier():
    usage = Usage(cache_read_tokens=1_000_000)
    assert cost_usd("anthropic", "claude-fable-5-1", usage, env=NO_ENV) == pytest.approx(0.25)
    assert cost_usd("anthropic", "claude-opus-5-5", usage, env=NO_ENV) == pytest.approx(0.20)


def test_dated_snapshot_prices_like_alias_but_prefixes_do_not_collide():
    assert price_for("anthropic", "claude-haiku-4-5-20251001", env=NO_ENV) == Price(1.0, 5.0)
    # claude-opus-5-5 must not pick up claude-opus-5's price by prefix
    assert price_for("anthropic", "claude-opus-5-5", env=NO_ENV).input == 4.0


def test_unknown_model_costs_none_not_zero():
    assert price_for("openai", "some-model", env=NO_ENV) is None
    assert cost_usd("openai", "some-model", Usage(input_tokens=10), env=NO_ENV) is None


def test_yaml_override_adds_and_replaces(tmp_path):
    f = tmp_path / "prices.yaml"
    f.write_text("openai:some-model: {input: 1.25, output: 10}\n"
                 "anthropic:claude-opus-5: {input: 4, output: 20, cache_read: 0.3, cache_write: 5}\n",
                 encoding="utf-8")
    env = {"AGENTKIT_PRICING_FILE": str(f)}
    assert cost_usd("openai", "some-model", Usage(input_tokens=1_000_000), env=env) == pytest.approx(1.25)
    opus = price_for("anthropic", "claude-opus-5", env=env)
    assert opus == Price(4.0, 20.0, cache_read=0.3, cache_write=5.0)
    # models not in the file still use the built-in table
    assert price_for("anthropic", "claude-sonnet-5", env=env) == Price(2.0, 10.0)


@pytest.mark.parametrize("body", [
    "- not a mapping\n",
    "no-colon-key: {input: 1, output: 2}\n",
    "openai:m: {input: 1}\n",
    "openai:m: {input: 1, output: 2, surprise: 3}\n",
    "openai:m: {input: cheap, output: 2}\n",
])
def test_malformed_pricing_file_is_rejected(tmp_path, body):
    f = tmp_path / "bad.yaml"
    f.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_pricing_file(f)


def test_missing_pricing_file_is_a_clear_error(tmp_path):
    env = {"AGENTKIT_PRICING_FILE": str(tmp_path / "missing.yaml")}
    with pytest.raises(ValueError, match="AGENTKIT_PRICING_FILE"):
        price_for("anthropic", "claude-opus-5", env=env)
