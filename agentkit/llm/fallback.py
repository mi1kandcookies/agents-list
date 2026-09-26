"""
agentkit/llm/fallback.py - try the next model when one fails or refuses.

FallbackAdapter wraps an ordered list of adapters and moves down the list when
a call raises ModelError or comes back with stop_reason "refusal":

    refusal, or a non-retryable error (bad key, bad request)
        the chain stays on the next model for the rest of the run, since the
        same request would fail the same way again
    retryable error (rate limit, 5xx, timeout)
        only this call moves on; the next call starts at the current model

Tokens spent on refused attempts are added to the usage of the response that
is returned, so budgets stay honest. If every model refuses, the last refusal
is returned for the loop to handle; if every model errors, one ModelError
names them all.

Claude Opus 5 / Opus 5.5 / Fable 5.1 / Mythos 5.1 can also fall back
server-side when agent.yaml opts in (see anthropic.py); this chain is the
cross-provider layer, from agent.yaml models.fallbacks or, in development,
AGENTKIT_FALLBACK_MODELS="openai:<id>,gemini:<id>".
"""
from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Mapping

from agentkit.errors import ModelError
from agentkit.llm.base import ModelAdapter, ModelRef, model_override_allowed
from agentkit.types import Message, ModelResponse, ToolSpec, Usage

FALLBACK_MODELS_ENV = "AGENTKIT_FALLBACK_MODELS"

# on_switch(from_ref, to_ref, reason) - e.g. to emit a "model_fallback" event
SwitchHook = Callable[[ModelRef, ModelRef, str], None]


class FallbackAdapter:
    def __init__(self, adapters: Iterable[ModelAdapter], *, on_switch: SwitchHook | None = None):
        self.adapters: list[ModelAdapter] = list(adapters)
        if not self.adapters:
            raise ValueError("FallbackAdapter needs at least one adapter")
        self.on_switch = on_switch
        self._start = 0      # index the next call starts from (sticky after refusals)
        self._served = 0     # index of the adapter that produced the last response

    @property
    def ref(self) -> ModelRef:
        return self.adapters[self._served].ref

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec],
                 max_tokens: int = 16000) -> ModelResponse:
        spent = Usage()
        refusal: ModelResponse | None = None
        errors: list[ModelError] = []
        last = len(self.adapters) - 1
        for i in range(self._start, len(self.adapters)):
            adapter = self.adapters[i]
            try:
                resp = adapter.complete(system=system, messages=messages, tools=tools,
                                        max_tokens=max_tokens)
            except ModelError as exc:
                errors.append(exc)
                if not exc.retryable and i < last:
                    self._start = i + 1
                self._switch(i, f"error: {exc}")
                continue
            if resp.stop_reason == "refusal" and i < last:
                spent = spent + resp.usage
                refusal = resp
                self._start = i + 1
                self._switch(i, "refusal")
                continue
            self._served = i
            resp.usage = spent + resp.usage if spent.total_tokens else resp.usage
            return resp
        if refusal is not None:
            # A later model errored after an earlier one refused: report the refusal.
            refusal.usage = spent
            return refusal
        raise ModelError("every model in the fallback chain failed: " +
                         "; ".join(str(e) for e in errors),
                         provider=errors[-1].provider if errors else "",
                         retryable=any(e.retryable for e in errors))

    def _switch(self, i: int, reason: str) -> None:
        if self.on_switch is not None and i + 1 < len(self.adapters):
            self.on_switch(self.adapters[i].ref, self.adapters[i + 1].ref, reason)


def parse_refs(value: "str | Iterable[str | ModelRef] | None") -> list[ModelRef]:
    """A comma-separated string or an iterable of refs -> ModelRefs."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    refs = []
    for item in items:
        if isinstance(item, ModelRef):
            refs.append(item)
        elif item and item.strip():
            refs.append(ModelRef.parse(item))
    return refs


def build_chain(primary_ref: "str | ModelRef",
                fallback_refs: "str | Iterable[str | ModelRef] | None" = None, *,
                env: Mapping[str, str] | None = None,
                clients: Mapping[str, Any] | None = None,
                options: Mapping[str, Mapping[str, Any]] | None = None,
                on_switch: SwitchHook | None = None) -> ModelAdapter:
    """The adapter for a primary model plus fallbacks.

    fallback_refs None reads AGENTKIT_FALLBACK_MODELS from `env` (or the
    process environment), but only when AGENTKIT_ALLOW_MODEL_OVERRIDE=1
    (development); otherwise there are none, so a production caller passes
    the manifest's (specialist.resolve_model_refs). `clients` maps
    "provider:model" to an injected SDK client; `options` maps a provider
    name to that provider's adapter options. Duplicates are dropped; with no
    fallbacks the primary adapter is returned unwrapped.
    """
    from agentkit.llm import get_adapter  # late import: agentkit.llm imports this module

    source = os.environ if env is None else env
    if fallback_refs is None:
        allowed = model_override_allowed(source)
        fallback_refs = source.get(FALLBACK_MODELS_ENV, "") if allowed else ""
    refs: list[ModelRef] = []
    for ref in parse_refs([primary_ref]) + parse_refs(fallback_refs):
        if ref not in refs:
            refs.append(ref)
    adapters = [get_adapter(ref, env=env, client=(clients or {}).get(str(ref)),
                            options=(options or {}).get(ref.provider)) for ref in refs]
    if len(adapters) == 1:
        return adapters[0]
    return FallbackAdapter(adapters, on_switch=on_switch)
