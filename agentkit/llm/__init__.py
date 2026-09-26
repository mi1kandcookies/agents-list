"""
agentkit/llm - model adapters behind one provider-neutral interface.

    ModelAdapter, ModelRef     the interface every adapter implements, and
                               "provider:model" references
    ScriptedAdapter            a deterministic, offline adapter for tests and evals

ScriptedAdapter is constructed directly (it needs a script, not a ref).
"""
from __future__ import annotations

from agentkit.llm.base import PROVIDERS, ModelAdapter, ModelRef
from agentkit.llm.scripted import ScriptedAdapter

__all__ = ["PROVIDERS", "ModelAdapter", "ModelRef", "ScriptedAdapter"]
