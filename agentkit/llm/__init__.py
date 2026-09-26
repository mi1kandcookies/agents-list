"""
agentkit/llm - model adapters behind one provider-neutral interface.

    get_adapter("anthropic:claude-opus-5")       official anthropic SDK
    get_adapter("openai:<model>")                official openai SDK
    get_adapter("gemini:<model>")                Gemini's OpenAI-compatible endpoint
    get_adapter("ollama:<model>") / "vllm:" / "openrouter:" / "openai_compat:"
    build_chain(primary, fallbacks)              FallbackAdapter across models/providers

Provider SDKs are imported lazily, so importing agentkit never requires them.
ScriptedAdapter is constructed directly (it needs a script, not a ref).
"""
from __future__ import annotations

from typing import Any, Mapping

from agentkit.llm.base import PROVIDERS, ModelAdapter, ModelRef
from agentkit.llm.fallback import FallbackAdapter, build_chain
from agentkit.llm.scripted import ScriptedAdapter


def get_adapter(ref: "str | ModelRef", *, env: Mapping[str, str] | None = None,
                client: Any = None, options: Mapping[str, Any] | None = None) -> ModelAdapter:
    """Build the adapter for `ref`. `client` injects a pre-built SDK client
    (tests pass a fake); `options` carries provider settings from the manifest."""
    ref = ModelRef.parse(ref) if isinstance(ref, str) else ref
    if ref.provider == "scripted":
        raise ValueError("construct ScriptedAdapter directly; it needs a script")
    if ref.provider == "anthropic":
        from agentkit.llm.anthropic import AnthropicAdapter
        return AnthropicAdapter(ref, env=env, client=client, options=options)
    from agentkit.llm.openai_compat import OpenAICompatAdapter
    return OpenAICompatAdapter(ref, env=env, client=client, options=options)


__all__ = ["PROVIDERS", "FallbackAdapter", "ModelAdapter", "ModelRef", "ScriptedAdapter",
           "build_chain", "get_adapter"]
