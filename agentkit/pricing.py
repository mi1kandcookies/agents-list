"""
agentkit/pricing.py - token prices used to turn model usage into USD.

The built-in table covers the Claude models the kit defaults to and the
models Anthropic's server-side fallbacks route to (a fallback turn, and the
sticky turns after it, are billed at the serving model's price). Prices are
USD per million tokens, taken from Anthropic's published model reference
(as of 2026-06-24; re-check before relying on them for billing). Cache
reads default to 0.1x the input price and cache writes (5-minute TTL) to
1.25x; models whose cache-read price differs carry it explicitly.

Anything else - OpenAI, Gemini, a local model, or a price change - comes from
a YAML file named by AGENTKIT_PRICING_FILE, keyed by "provider:model":

    openai:some-model:   {input: 1.25, output: 10}
    anthropic:claude-opus-5: {input: 5, output: 25, cache_read: 0.5, cache_write: 6.25}

File entries replace built-in ones. A model with no known price costs None
(unknown), never 0, so budgets and estimates can tell "free" from "unpriced".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agentkit.types import Usage

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25
PRICING_FILE_ENV = "AGENTKIT_PRICING_FILE"


@dataclass(frozen=True)
class Price:
    """USD per million tokens. cache_* None means "derive from input"."""
    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None

    @property
    def cache_read_rate(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * CACHE_READ_MULTIPLIER

    @property
    def cache_write_rate(self) -> float:
        return self.cache_write if self.cache_write is not None else self.input * CACHE_WRITE_MULTIPLIER

    def cost(self, usage: Usage) -> float:
        return (usage.input_tokens * self.input
                + usage.output_tokens * self.output
                + usage.cache_read_tokens * self.cache_read_rate
                + usage.cache_write_tokens * self.cache_write_rate) / 1_000_000


# Anthropic model reference as of 2026-06-24.
BUILTIN_PRICES: dict[str, Price] = {
    "anthropic:claude-opus-5":   Price(5.0, 25.0),
    "anthropic:claude-sonnet-5": Price(2.0, 10.0),
    "anthropic:claude-haiku-4-5": Price(1.0, 5.0),
    # Opus 5.5, Fable 5.1 and Mythos 5.1 have discounted cache reads.
    "anthropic:claude-opus-5-5": Price(4.0, 20.0, cache_read=0.20),
    "anthropic:claude-fable-5-1": Price(10.0, 50.0, cache_read=0.25),
    "anthropic:claude-mythos-5-1": Price(10.0, 50.0, cache_read=0.25),
    "anthropic:claude-fable-5": Price(10.0, 50.0),
    "anthropic:claude-mythos-5": Price(10.0, 50.0),
    # Earlier models still served; claude-opus-4-8 is the default target of
    # server-side fallbacks from Opus 5 / Fable 5.1 (cyber refusals).
    "anthropic:claude-opus-4-8": Price(5.0, 25.0),
    "anthropic:claude-opus-4-7": Price(5.0, 25.0),
    "anthropic:claude-opus-4-6": Price(5.0, 25.0),
    "anthropic:claude-sonnet-4-6": Price(3.0, 15.0),
}

# Pinned snapshots ("claude-haiku-4-5-20251001") price like their alias.
_DATE_SUFFIX = re.compile(r"-\d{8}$")
_PRICE_KEYS = {"input", "output", "cache_read", "cache_write"}

# (path, mtime) -> parsed table, so a long run does not re-read the file per call.
_file_cache: dict[tuple[str, float], dict[str, Price]] = {}


def load_pricing_file(path: str | Path) -> dict[str, Price]:
    """Parse a pricing YAML file. Raises ValueError on a malformed entry."""
    import yaml  # PyYAML; only needed when an override file is configured

    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: pricing file must be a mapping of 'provider:model' to prices")
    table: dict[str, Price] = {}
    for key, entry in data.items():
        if not isinstance(key, str) or ":" not in key:
            raise ValueError(f"{path}: key {key!r} must look like 'provider:model'")
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: {key}: expected a mapping with input/output prices")
        unknown = set(entry) - _PRICE_KEYS
        if unknown:
            raise ValueError(f"{path}: {key}: unknown keys {sorted(unknown)}")
        try:
            table[_normalize(key)] = Price(
                input=float(entry["input"]),
                output=float(entry["output"]),
                cache_read=_opt_float(entry.get("cache_read")),
                cache_write=_opt_float(entry.get("cache_write")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {key}: needs numeric input and output ({exc})") from None
    return table


def price_for(provider: str, model: str, *, env: Mapping[str, str] | None = None) -> Price | None:
    """The price for provider:model, override file first, else None."""
    key = _normalize(f"{provider}:{model}")
    path = (os.environ if env is None else env).get(PRICING_FILE_ENV, "").strip()
    if path:
        overrides = _cached_file(path)
        if key in overrides:
            return overrides[key]
    return BUILTIN_PRICES.get(key)


def cost_usd(provider: str, model: str, usage: Usage, *,
             env: Mapping[str, str] | None = None) -> float | None:
    """USD for one call's usage, or None when the model has no known price."""
    price = price_for(provider, model, env=env)
    return None if price is None else price.cost(usage)


def _normalize(key: str) -> str:
    provider, _, model = key.strip().partition(":")
    return f"{provider.strip().lower()}:{_DATE_SUFFIX.sub('', model.strip())}"


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _cached_file(path: str) -> dict[str, Price]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        raise ValueError(f"{PRICING_FILE_ENV} points at {path!r}, which cannot be read") from None
    key = (path, mtime)
    if key not in _file_cache:
        _file_cache.clear()
        _file_cache[key] = load_pricing_file(path)
    return _file_cache[key]
