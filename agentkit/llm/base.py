"""
agentkit/llm/base.py - the provider-neutral model interface.

A ModelAdapter turns (system prompt, neutral history, tool specs) into one
ModelResponse. Adapters own every provider detail: wire format, replaying
provider-native assistant content, stop-reason mapping, usage and cost.
The loop never sees a provider SDK type.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from agentkit.types import Message, ModelResponse, ToolSpec

# anthropic      Claude via the official `anthropic` SDK
# openai, gemini, openrouter, ollama, vllm, openai_compat
#                OpenAI-compatible Chat Completions via the official `openai` SDK
# scripted       deterministic offline adapter for tests and evals
PROVIDERS = ("anthropic", "openai", "gemini", "openrouter", "ollama", "vllm",
             "openai_compat", "scripted")
# Development switch: only with it set to "1" do --model and the AGENTKIT_*
# model variables replace the manifest's (stamped) models.
ALLOW_MODEL_OVERRIDE_ENV = "AGENTKIT_ALLOW_MODEL_OVERRIDE"


def model_override_allowed(env: Mapping[str, str] | None = None) -> bool:
    """True only when AGENTKIT_ALLOW_MODEL_OVERRIDE is "1" (development)."""
    env = os.environ if env is None else env
    return env.get(ALLOW_MODEL_OVERRIDE_ENV) == "1"


@dataclass(frozen=True)
class ModelRef:
    """A model reference written as "provider:model", e.g. "anthropic:claude-opus-5".

    Only the first ':' separates provider from model, so model ids that
    contain colons (e.g. "ollama:qwen3:32b") survive.
    """
    provider: str
    model: str

    @classmethod
    def parse(cls, ref: str) -> "ModelRef":
        provider, sep, model = (ref or "").strip().partition(":")
        provider = provider.strip().lower()
        model = model.strip()
        if not sep or not provider or not model:
            raise ValueError(f"model ref must look like 'provider:model', got {ref!r}")
        if provider not in PROVIDERS:
            raise ValueError(f"unknown model provider {provider!r}; expected one of {', '.join(PROVIDERS)}")
        return cls(provider, model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@runtime_checkable
class ModelAdapter(Protocol):
    ref: ModelRef

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 16000) -> ModelResponse:
        """Run one model turn. Raises agentkit.errors.ModelError on failure."""
        ...
