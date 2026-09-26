"""
agentkit/llm/anthropic.py - Claude through the official `anthropic` SDK.

Request shape (one Messages API call per turn):

    thinking      {"type": "adaptive"} on models that support it (not Haiku 4.5
                  or older), with output_config {"effort": options.effort or "high"}
    cache_control top-level {"type": "ephemeral"}, so the growing history is
                  cached turn over turn
    fallbacks     "default" (beta server-side-fallback-2026-07-01) on Claude
                  Opus 5, Opus 5.5, Fable 5.1 and Mythos 5.1 only when
                  options.server_fallbacks is true: a policy decline is then
                  re-run server-side on Anthropic's recommended model instead
                  of coming back as a refusal. Off by default, so a run stays
                  on the model the operator stamped; the opt-in lives in
                  agent.yaml, which spec_hash covers
    no prefill and no forced tool_choice (newer models reject both)

History: assistant turns are replayed from Message.raw (the provider-native
content, thinking blocks included, unchanged) whenever it came from Claude,
even another Claude model (after a fallback or a --model switch on resume):
the API drops thinking the target model cannot read, unbilled, and
stripping blocks by hand can break ordering and signatures. Turns from other
providers are rebuilt from text + tool_calls. All tool results of a turn go
back in ONE user message of tool_result blocks. A tool_use block
with no tool_result after it (a refused turn's partial call, seen on
resume) is left out, as the API requires every tool_use to be answered.

Served-by: a response that a fallback model served lists each switch in
ModelResponse.fallbacks as {"from", "to"} refs - one per "fallback" content
block, or, on a sticky turn (served by the fallback model directly, with no
block), the requested model and the one the response names - and the loop
journals each as a model_fallback event.

Options (agent.yaml models.options.anthropic, or get_adapter(options=...)):
effort, server_fallbacks (true to opt in), thinking (False to omit it),
timeout, max_retries. The key comes from ANTHROPIC_API_KEY.
"""
from __future__ import annotations

from typing import Any, Mapping

from agentkit import pricing
from agentkit.errors import ModelError
from agentkit.llm.base import ModelRef
from agentkit.types import Message, ModelResponse, ToolCall, ToolSpec, Usage

# Models that take thinking {"type": "adaptive"} and output_config.effort.
# Prefix match, so dated snapshots and point releases (claude-opus-5-5) count.
ADAPTIVE_THINKING_PREFIXES = (
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)
# Models whose refusals the API can re-run server-side with fallbacks="default".
SERVER_FALLBACK_MODELS = ("claude-opus-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1")
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_EFFORT = "high"

_STOP_REASONS = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause",
    "compaction": "pause",
}
# Blocks that belong to a model that declined mid-turn; never echoed back.
_DECLINED_PARTIAL_TYPES = {"thinking", "redacted_thinking", "tool_use"}


def supports_adaptive_thinking(model: str) -> bool:
    return model.startswith(ADAPTIVE_THINKING_PREFIXES)


def _sdk():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - requirements-agents.txt installs it
        raise ModelError("the anthropic SDK is not installed; "
                         "pip install -r requirements-agents.txt",
                         provider="anthropic") from exc
    return anthropic


class AnthropicAdapter:
    def __init__(self, ref: ModelRef, *, env: Mapping[str, str] | None = None,
                 client: Any = None, options: Mapping[str, Any] | None = None):
        if ref.provider != "anthropic":
            raise ValueError(f"AnthropicAdapter needs an anthropic: ref, got {ref}")
        self.ref = ref
        self.env = env
        self.options = dict(options or {})
        self._client = client

    # --- client ---------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The SDK client, built on first use so constructing an adapter needs no key."""
        if self._client is None:
            sdk = _sdk()
            kwargs: dict[str, Any] = {}
            key = (self.env or {}).get("ANTHROPIC_API_KEY")
            if key:
                kwargs["api_key"] = key
            for opt in ("timeout", "max_retries"):
                if self.options.get(opt) is not None:
                    kwargs[opt] = self.options[opt]
            self._client = sdk.Anthropic(**kwargs)
        return self._client

    # --- request --------------------------------------------------------------

    def uses_server_fallbacks(self) -> bool:
        """Only with an explicit server_fallbacks: true, so by default every
        turn runs on the requested (stamped) model or comes back refused."""
        return self.options.get("server_fallbacks") is True and \
            self.ref.model in SERVER_FALLBACK_MODELS

    def build_request(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                      max_tokens: int = 16000) -> dict[str, Any]:
        """The exact kwargs for messages.create (or beta.messages.create)."""
        req: dict[str, Any] = {
            "model": self.ref.model,
            "max_tokens": max_tokens,
            "messages": self.to_wire(messages),
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            req["system"] = system
        if tools:
            req["tools"] = [{"name": t.name, "description": t.description,
                             "input_schema": t.input_schema} for t in tools]
        if supports_adaptive_thinking(self.ref.model):
            if self.options.get("thinking", True):
                req["thinking"] = {"type": "adaptive"}
            req["output_config"] = {"effort": self.options.get("effort") or DEFAULT_EFFORT}
        if self.uses_server_fallbacks():
            req["betas"] = [SERVER_FALLBACK_BETA]
            req["fallbacks"] = "default"
        return req

    def to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Neutral history -> Messages API `messages` (user/assistant alternation)."""
        wire: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if msg.role == "assistant":
                content = self._assistant_content(msg, _answered(messages, i))
                if content:  # the API rejects an empty assistant turn
                    wire.append({"role": "assistant", "content": content})
                continue
            if msg.role == "tool":
                blocks: list[dict[str, Any]] = []
                for r in msg.tool_results:
                    block = {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content}
                    if r.is_error:
                        block["is_error"] = True
                    blocks.append(block)
            else:
                blocks = [{"type": "text", "text": msg.text}] if msg.text else []
            if not blocks:
                continue
            # Consecutive user-side turns (tool results, then a nudge) become one
            # user message; tool_result blocks stay first, as the API requires.
            if wire and wire[-1]["role"] == "user":
                wire[-1]["content"].extend(blocks)
            else:
                wire.append({"role": "user", "content": blocks})
        return wire

    def _assistant_content(self, msg: Message, answered: set[str]) -> list[dict[str, Any]]:
        raw = msg.raw or {}
        if raw.get("provider") == "anthropic" and isinstance(raw.get("content"), list):
            blocks = _echoable(raw["content"])
        else:
            blocks = []
            if msg.text:
                blocks.append({"type": "text", "text": msg.text})
            for call in msg.tool_calls:
                blocks.append({"type": "tool_use", "id": call.id, "name": call.name,
                               "input": dict(call.arguments)})
        return [b for b in blocks if b.get("type") != "tool_use" or b.get("id") in answered]

    # --- call -----------------------------------------------------------------

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 16000) -> ModelResponse:
        req = self.build_request(system=system, messages=messages, tools=tools,
                                 max_tokens=max_tokens)
        sdk = _sdk()
        try:
            if "betas" in req:
                resp = self.client.beta.messages.create(**req)
            else:
                resp = self.client.messages.create(**req)
        except sdk.AnthropicError as exc:
            raise _model_error(exc) from exc
        return self.parse_response(resp)

    def parse_response(self, resp: Any) -> ModelResponse:
        content = [_block_dict(b) for b in (resp.content or [])]
        # After a server-side fallback only the blocks past the last boundary
        # are the serving model's answer.
        tail = content[_last_fallback_index(content) + 1:]
        stop = _STOP_REASONS.get(resp.stop_reason or "end_turn", "end")
        text = "".join(b.get("text", "") for b in tail if b.get("type") == "text")
        calls = [ToolCall(id=b["id"], name=b["name"], arguments=dict(b.get("input") or {}))
                 for b in tail if b.get("type") == "tool_use"]
        if stop == "refusal":
            calls = []  # a declined turn's partial output is not acted on
        raw: dict[str, Any] = {"provider": "anthropic", "model": self.ref.model, "content": content}
        details = getattr(resp, "stop_details", None)
        if details is not None:
            raw["stop_details"] = _block_dict(details)
        served = resp.model or self.ref.model
        return ModelResponse(text=text, tool_calls=calls, stop_reason=stop,
                             usage=self._usage(resp), model=served, raw=raw,
                             fallbacks=self._switches(content, resp, served))

    def _switches(self, content: list[dict[str, Any]], resp: Any, served: str) -> list[dict[str, str]]:
        """Server-side model switches in this response (see the module docstring)."""
        switches = [{"from": _anthropic_ref(b.get("from")), "to": _anthropic_ref(b.get("to"))}
                    for b in content if b.get("type") == "fallback"]
        iterations = getattr(resp.usage, "iterations", None) or []
        sticky = any(getattr(it, "type", None) == "fallback_message" for it in iterations)
        if not switches and sticky and served != self.ref.model:
            switches = [{"from": str(self.ref), "to": f"anthropic:{served}"}]
        return switches

    def _usage(self, resp: Any) -> Usage:
        u = resp.usage
        iterations = getattr(u, "iterations", None) or []
        # With iterations (server-side fallback, server tool loops) each entry
        # is one billed attempt, possibly on another model; top-level usage
        # covers only the attempt that produced the message. An entry without
        # a model ran on the requested model until a fallback_message entry
        # appears, and on the serving model from then on.
        parts, fell_back = [], False
        for it in iterations:
            fell_back = fell_back or getattr(it, "type", None) == "fallback_message"
            default = (resp.model or self.ref.model) if fell_back else self.ref.model
            parts.append((getattr(it, "model", None) or default, it))
        parts = parts or [(resp.model or self.ref.model, u)]
        total = Usage()
        for model, part in parts:
            usage = Usage(input_tokens=part.input_tokens or 0,
                          output_tokens=part.output_tokens or 0,
                          cache_read_tokens=getattr(part, "cache_read_input_tokens", None) or 0,
                          cache_write_tokens=getattr(part, "cache_creation_input_tokens", None) or 0)
            usage.cost_usd = pricing.cost_usd("anthropic", str(model), usage, env=self.env)
            total = total + usage
        return total


def _answered(messages: list[Message], i: int) -> set[str]:
    """Call ids answered by the tool message right after messages[i]."""
    nxt = messages[i + 1] if i + 1 < len(messages) else None
    return {r.call_id for r in nxt.tool_results} if nxt is not None and nxt.role == "tool" else set()


def _block_dict(block: Any) -> dict[str, Any]:
    """An SDK model as the JSON the API sent (aliases such as "from" kept)."""
    if isinstance(block, dict):
        return dict(block)
    return block.model_dump(mode="json", by_alias=True, exclude_unset=True)


def _anthropic_ref(side: Any) -> str:
    """The "from" / "to" of a fallback block ({"model": ...}) as a model ref."""
    model = side.get("model") if isinstance(side, dict) else None
    return f"anthropic:{model}" if model else "anthropic:unknown"


def _last_fallback_index(content: list[dict[str, Any]]) -> int:
    idx = -1
    for i, block in enumerate(content):
        if block.get("type") == "fallback":
            idx = i
    return idx


def _echoable(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Raw assistant content ready to send back: fallback markers dropped, and
    thinking / tool_use blocks from a model that declined before the last
    boundary omitted. Everything else is passed back unchanged."""
    cut = _last_fallback_index(content)
    out = []
    for i, block in enumerate(content):
        if block.get("type") == "fallback":
            continue
        if i < cut and block.get("type") in _DECLINED_PARTIAL_TYPES:
            continue
        out.append(block)
    return out


def _model_error(exc: Exception) -> ModelError:
    status = getattr(exc, "status_code", None)
    if status is None:
        # Connection problems and timeouts carry no status and are worth a retry.
        sdk = _sdk()
        retryable = isinstance(exc, sdk.APIConnectionError)
    else:
        retryable = status in (408, 409, 429) or status >= 500
    label = f"HTTP {status}: " if status is not None else ""
    return ModelError(f"anthropic: {label}{exc}", provider="anthropic", retryable=retryable)
