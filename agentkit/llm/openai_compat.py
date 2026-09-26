"""
agentkit/llm/openai_compat.py - OpenAI-compatible Chat Completions through the
official `openai` SDK: OpenAI itself, Gemini's compatibility endpoint,
OpenRouter, and self-hosted servers (Ollama, vLLM, anything speaking the
same wire format).

Presets (provider part of the model ref):

    openai         OPENAI_API_KEY; SDK default base URL
    gemini         GEMINI_API_KEY; https://generativelanguage.googleapis.com/v1beta/openai/
    openrouter     OPENROUTER_API_KEY; https://openrouter.ai/api/v1
    ollama         OLLAMA_BASE_URL (default http://localhost:11434/v1); no key needed
    vllm           AGENTKIT_OPENAI_COMPAT_BASE_URL (+ optional _API_KEY)
    openai_compat  AGENTKIT_OPENAI_COMPAT_BASE_URL (+ optional _API_KEY)

Options: base_url, api_key_env, max_tokens_param ("max_completion_tokens" for
openai, "max_tokens" elsewhere), reasoning_effort, timeout, max_retries.

Tools are sent as function tools. Tool-call arguments arrive as a JSON
string; one that does not parse to an object becomes ToolCall.invalid, which
the loop answers with an error result instead of running the tool. Each
tool result goes back as its own "tool" message. A replayed tool call with
no result after it (a refused turn's partial call, seen on resume) is left
out, since the API rejects unanswered tool calls.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from agentkit import pricing
from agentkit.errors import ModelError
from agentkit.llm.base import ModelRef
from agentkit.types import Message, ModelResponse, ToolCall, ToolSpec, Usage

COMPAT_BASE_URL_ENV = "AGENTKIT_OPENAI_COMPAT_BASE_URL"
COMPAT_API_KEY_ENV = "AGENTKIT_OPENAI_COMPAT_API_KEY"
# Local servers ignore the key, but the SDK insists on a non-empty one.
_PLACEHOLDER_KEY = "not-needed"


@dataclass(frozen=True)
class Preset:
    key_env: str | None             # env var holding the API key
    key_required: bool
    base_url: str | None = None     # fixed default base URL (None = SDK default)
    base_url_env: str | None = None  # env var that overrides / supplies the base URL
    max_tokens_param: str = "max_tokens"


PRESETS: dict[str, Preset] = {
    "openai": Preset("OPENAI_API_KEY", True, max_tokens_param="max_completion_tokens"),
    "gemini": Preset("GEMINI_API_KEY", True,
                     base_url="https://generativelanguage.googleapis.com/v1beta/openai/"),
    "openrouter": Preset("OPENROUTER_API_KEY", True, base_url="https://openrouter.ai/api/v1"),
    "ollama": Preset(None, False, base_url="http://localhost:11434/v1",
                     base_url_env="OLLAMA_BASE_URL"),
    "vllm": Preset(COMPAT_API_KEY_ENV, False, base_url_env=COMPAT_BASE_URL_ENV),
    "openai_compat": Preset(COMPAT_API_KEY_ENV, False, base_url_env=COMPAT_BASE_URL_ENV),
}

_FINISH_REASONS = {
    "stop": "end",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}
# Assistant-message keys worth echoing back. tool_calls is kept verbatim so
# provider extras on a call (e.g. Gemini's thought signatures) survive;
# reasoning_details is how OpenRouter carries reasoning across turns.
_REPLAY_KEYS = ("role", "content", "tool_calls", "refusal", "reasoning_details")


def _sdk():
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - requirements-agents.txt installs it
        raise ModelError("the openai SDK is not installed; pip install -r requirements-agents.txt",
                         provider="openai") from exc
    return openai


class OpenAICompatAdapter:
    def __init__(self, ref: ModelRef, *, env: Mapping[str, str] | None = None,
                 client: Any = None, options: Mapping[str, Any] | None = None):
        if ref.provider not in PRESETS:
            raise ValueError(f"OpenAICompatAdapter does not serve provider {ref.provider!r}")
        self.ref = ref
        self.preset = PRESETS[ref.provider]
        self.env = env
        self.options = dict(options or {})
        self._client = client

    # --- client ---------------------------------------------------------------

    def _env(self) -> Mapping[str, str]:
        return os.environ if self.env is None else self.env

    def client_kwargs(self) -> dict[str, Any]:
        """api_key / base_url (and timeout, max_retries) for openai.OpenAI()."""
        env, p = self._env(), self.preset
        key_env = self.options.get("api_key_env") or p.key_env
        key = (env.get(key_env) or "").strip() if key_env else ""
        if not key:
            if p.key_required:
                raise ModelError(f"{self.ref.provider}: set {key_env} to use {self.ref}",
                                 provider=self.ref.provider)
            key = _PLACEHOLDER_KEY
        base_url = self.options.get("base_url") or \
            ((env.get(p.base_url_env) or "").strip() if p.base_url_env else "") or p.base_url
        if not base_url and self.ref.provider in ("vllm", "openai_compat"):
            raise ModelError(f"{self.ref.provider}: set {COMPAT_BASE_URL_ENV} to the server's "
                             "OpenAI-compatible /v1 URL", provider=self.ref.provider)
        kwargs: dict[str, Any] = {"api_key": key}
        if base_url:
            kwargs["base_url"] = base_url
        for opt in ("timeout", "max_retries"):
            if self.options.get(opt) is not None:
                kwargs[opt] = self.options[opt]
        return kwargs

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _sdk().OpenAI(**self.client_kwargs())
        return self._client

    # --- request --------------------------------------------------------------

    def build_request(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                      max_tokens: int = 16000) -> dict[str, Any]:
        """The exact kwargs for client.chat.completions.create."""
        wire: list[dict[str, Any]] = []
        if system:
            wire.append({"role": "system", "content": system})
        wire.extend(self.to_wire(messages))
        param = self.options.get("max_tokens_param") or self.preset.max_tokens_param
        req: dict[str, Any] = {"model": self.ref.model, "messages": wire, param: max_tokens}
        if tools:
            req["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools]
        if self.options.get("reasoning_effort"):
            req["reasoning_effort"] = self.options["reasoning_effort"]
        return req

    def to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if msg.role == "user":
                wire.append({"role": "user", "content": msg.text})
            elif msg.role == "tool":
                for r in msg.tool_results:
                    content = f"Error: {r.content}" if r.is_error else r.content
                    wire.append({"role": "tool", "tool_call_id": r.call_id, "content": content})
            else:
                nxt = messages[i + 1] if i + 1 < len(messages) else None
                answered = {r.call_id for r in nxt.tool_results} \
                    if nxt is not None and nxt.role == "tool" else set()
                turn = self._assistant_message(msg, answered)
                if turn:
                    wire.append(turn)
        return wire

    def _assistant_message(self, msg: Message, answered: set[str]) -> dict[str, Any] | None:
        raw = msg.raw or {}
        if raw.get("provider") == self.ref.provider and raw.get("model") == self.ref.model \
                and isinstance(raw.get("content"), dict):
            turn = {k: v for k, v in raw["content"].items() if k in _REPLAY_KEYS and v is not None}
            calls = [c for c in turn.get("tool_calls") or [] if c.get("id") in answered]
        else:
            turn = {"role": "assistant", "content": msg.text or None}
            calls = [{"id": c.id, "type": "function", "function": {
                "name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in msg.tool_calls if c.id in answered]
        turn.pop("tool_calls", None)
        if calls:
            turn["tool_calls"] = calls
        if not turn.get("content") and not turn.get("refusal") and not calls:
            return None
        turn["role"] = "assistant"
        return turn

    # --- call -----------------------------------------------------------------

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 16000) -> ModelResponse:
        req = self.build_request(system=system, messages=messages, tools=tools,
                                 max_tokens=max_tokens)
        sdk = _sdk()
        try:
            resp = self.client.chat.completions.create(**req)
        except sdk.OpenAIError as exc:
            raise _model_error(exc, self.ref.provider) from exc
        return self.parse_response(resp)

    def parse_response(self, resp: Any) -> ModelResponse:
        if not getattr(resp, "choices", None):
            raise ModelError(f"{self.ref.provider}: response had no choices",
                             provider=self.ref.provider, retryable=True)
        choice = resp.choices[0]
        msg = choice.message
        calls = [_tool_call(tc) for tc in (msg.tool_calls or [])]
        stop = _FINISH_REASONS.get(choice.finish_reason or "stop", "end")
        # Some compatible servers report "stop" even when they return tool calls.
        if calls and stop == "end":
            stop = "tool_use"
        text = msg.content or ""
        if msg.refusal and not calls:
            text = text or msg.refusal
            stop = "refusal"
        if stop == "refusal":
            calls = []
        content = msg.model_dump(mode="json", exclude_unset=True)
        raw = {"provider": self.ref.provider, "model": self.ref.model, "content": content}
        return ModelResponse(text=text, tool_calls=calls, stop_reason=stop,
                             usage=self._usage(resp), model=resp.model or self.ref.model, raw=raw)

    def _usage(self, resp: Any) -> Usage:
        u = getattr(resp, "usage", None)
        if u is None:
            return Usage()
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", None) or 0) if details else 0
        written = (getattr(details, "cache_write_tokens", None) or 0) if details else 0
        # prompt_tokens counts cached tokens too; Usage.input_tokens is the uncached part.
        usage = Usage(input_tokens=max((u.prompt_tokens or 0) - cached - written, 0),
                      output_tokens=u.completion_tokens or 0,
                      cache_read_tokens=cached, cache_write_tokens=written)
        usage.cost_usd = pricing.cost_usd(self.ref.provider, self.ref.model, usage, env=self.env)
        return usage


def _tool_call(tc: Any) -> ToolCall:
    fn = getattr(tc, "function", None)
    if getattr(tc, "type", "function") != "function" or fn is None:
        name = getattr(getattr(tc, "custom", None), "name", "") or ""
        return ToolCall(id=tc.id, name=name, arguments={},
                        invalid=f"unsupported tool call type {getattr(tc, 'type', '?')!r}")
    text = fn.arguments or ""
    if not text.strip():
        return ToolCall(id=tc.id, name=fn.name, arguments={})
    try:
        args = json.loads(text)
    except json.JSONDecodeError as exc:
        return ToolCall(id=tc.id, name=fn.name, arguments={},
                        invalid=f"arguments are not valid JSON ({exc.msg} at char {exc.pos}): "
                                f"{text[:500]}")
    if not isinstance(args, dict):
        return ToolCall(id=tc.id, name=fn.name, arguments={},
                        invalid=f"arguments must be a JSON object, got {type(args).__name__}")
    return ToolCall(id=tc.id, name=fn.name, arguments=args)


def _model_error(exc: Exception, provider: str) -> ModelError:
    status = getattr(exc, "status_code", None)
    if status is None:
        retryable = isinstance(exc, _sdk().APIConnectionError)
    else:
        retryable = status in (408, 409, 429) or status >= 500
    label = f"HTTP {status}: " if status is not None else ""
    return ModelError(f"{provider}: {label}{exc}", provider=provider, retryable=retryable)
