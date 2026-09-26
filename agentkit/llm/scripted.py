"""
agentkit/llm/scripted.py - a deterministic, offline ModelAdapter.

Tests and evals drive the real loop, tools and checks with a scripted model:
each step is either a ModelResponse or a function of the history that returns
one. `from_tool_plan` builds the common case, a fixed sequence of tool calls:

    ScriptedAdapter.from_tool_plan([
        ("write_file", {"path": "deliverables/m1/report.md", "content": "..."}),
        [("record_source", {...}), ("record_claim", {...})],   # parallel calls
        ("submit_milestone", {"summary": "done", "artifacts": [...]}),
    ])
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Iterable, Union

from agentkit.llm.base import ModelRef
from agentkit.types import Message, ModelResponse, ToolCall, ToolSpec, Usage

Step = Union[ModelResponse, Callable[[list[Message]], ModelResponse]]
PlanItem = Union[str, tuple[str, dict], list[tuple[str, dict]]]


class ScriptedAdapter:
    def __init__(self, steps: Iterable[Step], *, model: str = "scripted",
                 usage_per_step: Usage | None = None):
        self.ref = ModelRef("scripted", model)
        self._steps: list[Step] = list(steps)
        self._usage = usage_per_step or Usage(input_tokens=100, output_tokens=50)
        # Every call as {"system", "messages" (dicts), "tools" (names), "max_tokens"}.
        self.calls: list[dict[str, Any]] = []

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 16000) -> ModelResponse:
        self.calls.append({
            "system": system,
            "messages": [m.to_dict() for m in messages],
            "tools": [t.name for t in tools],
            "max_tokens": max_tokens,
        })
        if not self._steps:
            return ModelResponse(text="(script exhausted)", tool_calls=[], stop_reason="end",
                                 usage=Usage(), model=self.ref.model)
        step = self._steps.pop(0)
        resp = step(copy.deepcopy(messages)) if callable(step) else step
        return resp

    @classmethod
    def from_tool_plan(cls, plan: Iterable[PlanItem], **kwargs: Any) -> "ScriptedAdapter":
        """A str is a final text answer; a (tool, args) tuple is one call; a
        list of tuples is several calls in the same turn."""
        usage = kwargs.get("usage_per_step") or Usage(input_tokens=100, output_tokens=50)
        model = kwargs.get("model", "scripted")
        steps: list[Step] = []
        n = 0
        for item in plan:
            if isinstance(item, str):
                steps.append(ModelResponse(text=item, tool_calls=[], stop_reason="end",
                                           usage=usage, model=model))
                continue
            group = item if isinstance(item, list) else [item]
            calls = []
            for name, args in group:
                n += 1
                calls.append(ToolCall(id=f"call_{n}", name=name, arguments=dict(args)))
            steps.append(ModelResponse(text="", tool_calls=calls, stop_reason="tool_use",
                                       usage=usage, model=model))
        return cls(steps, **kwargs)
