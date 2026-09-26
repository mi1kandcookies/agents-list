"""
agentkit/loop.py - the Runner: think -> act -> observe until the milestone
is submitted or a limit is hit.

    - History is append-only: model turns are never edited or reordered, so a
      provider's native content (e.g. Claude thinking blocks) replays intact.
      The one exception is a refused final turn, which is dropped when the
      run is resumed (a declined partial is discarded, never replayed).
    - All tool results of one model turn go back in ONE tool message, and
      every call of a turn gets a result, even when a tool stops the run
      (BudgetExceeded): calls not run are answered with error results.
    - Limits (steps, tokens, USD, wall minutes) are checked before every model
      call; hitting one raises BudgetExceeded, which run() turns into an
      outcome with status "budget_exceeded". max_tokens meters uncached
      input, cache writes and output; cache reads (the whole cached prefix,
      re-read every turn) are governed by max_usd instead.
    - A model with no known price adds no cost, so max_usd cannot bind on
      its tokens; the first such response emits a "cost_unknown" event.
    - Stuck detector: the same set of calls three turns in a row earns a
      nudge; five in a row stops the run with status "stuck".
    - If the model ends its turn without calling submit_milestone it gets one
      nudge; a second stop ends the run with status "no_submission".
    - A reply cut off at the output limit is never acted on: its tool calls
      (whose arguments may be truncated) are answered with error results
      and the model is asked to continue in smaller steps.
    - stop_reason "pause" (a provider paused a long turn) just continues.
    - The models that produced turns are recorded ("provider:model", first
      use first; RunOutcome.models), and every provider-side switch a
      response reports (ModelResponse.fallbacks, e.g. a Claude server-side
      fallback) is journaled as a "model_fallback" event.
    - A model call that fails after the SDK's retries (ModelError) ends the
      run with status "model_error"; the history is left consistent, so the
      run can be resumed.
    - A checkpoint is saved after every step; run(resume=True) continues from
      it with the same history, usage and elapsed time. A submitted
      milestone is not re-run. Resuming a run that stopped (limit, stuck,
      no submission, refusal, model error) appends one user message saying
      so, so the history never ends on an assistant turn (a refused final
      turn is dropped first); client answers that arrived since the
      checkpoint are delivered in a user message too.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agentkit.errors import BudgetExceeded, ModelError
from agentkit.events import EventSink, NullSink
from agentkit.journal import Checkpoint, Journal
from agentkit.llm.base import ModelAdapter
from agentkit.security import wrap_untrusted
from agentkit.tools.base import ToolContext, ToolRegistry
from agentkit.types import Message, ModelResponse, ToolCall, ToolResult, Usage

STUCK_NUDGE_AT = 3
STUCK_STOP_AT = 5
NUDGE_NO_SUBMIT = ("You ended your turn without calling submit_milestone. If the deliverables "
                   "are complete, call submit_milestone with a summary and the artifact paths. "
                   "If you are blocked, call ask_client, then finish what you can and submit.")
NUDGE_STUCK = ("You have made the same tool call(s) {n} times in a row. Change your approach: "
               "read the last result, try different arguments or a different tool, or submit "
               "what you have.")
NUDGE_CUT_OFF = "Your reply hit the output limit. Continue where you left off, in smaller steps."
CUT_OFF_CALL = ("Not run: your reply hit the output token limit while writing this call, so its "
                "arguments may be incomplete. Make the call again with less content per call "
                "(for a large file: write_file a first part, then edit_file to add the rest).")
RESUME_STOPPED = ("This milestone is resuming after the run stopped ({status}). Review where you "
                  "are, continue the work, and call submit_milestone when the deliverables are "
                  "complete.")
NOT_RUN_BUDGET = "Not run: the run stopped at a limit ({limit}) before this call."


@dataclass
class Limits:
    max_steps: int = 80
    max_tokens: int = 3_000_000     # uncached input + cache writes + output, summed over calls
    max_usd: float = 40.0
    max_wall_minutes: float = 180.0

    @classmethod
    def from_manifest(cls, manifest: Any) -> "Limits":
        lim = manifest.limits
        return cls(max_steps=lim.max_steps, max_tokens=lim.max_tokens, max_usd=lim.max_usd,
                   max_wall_minutes=lim.max_wall_minutes)


@dataclass
class RunOutcome:
    # submitted | no_submission | budget_exceeded | stuck | refused | model_error
    status: str
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    questions: list[str] = field(default_factory=list)
    steps: int = 0
    limit: str | None = None       # which limit, when status is budget_exceeded
    models: list[str] = field(default_factory=list)   # "provider:model" that produced turns


def metered_tokens(usage: Usage) -> int:
    """Tokens that count toward Limits.max_tokens (cache reads excluded)."""
    return usage.input_tokens + usage.cache_write_tokens + usage.output_tokens


def _signature(calls: list[ToolCall]) -> str:
    return json.dumps([[c.name, c.arguments, c.invalid] for c in calls], sort_keys=True, default=str)


class Runner:
    def __init__(self, adapter: ModelAdapter, registry: ToolRegistry, *,
                 events: EventSink | None = None, journal: Journal | None = None,
                 limits: Limits | None = None, max_tokens_per_call: int = 16000,
                 clock: Callable[[], float] = time.monotonic):
        self.adapter = adapter
        self.registry = registry
        self.events = events or NullSink()
        self.journal = journal
        self.limits = limits or Limits()
        self.max_tokens_per_call = max_tokens_per_call
        self.clock = clock

    # --- limits -----------------------------------------------------------------

    def check_limits(self, cp: Checkpoint, elapsed: float) -> None:
        lim = self.limits
        if cp.step >= lim.max_steps:
            raise BudgetExceeded("max_steps", f"reached {lim.max_steps} steps")
        if metered_tokens(cp.usage) >= lim.max_tokens:
            raise BudgetExceeded("max_tokens", f"used {metered_tokens(cp.usage)} tokens")
        if cp.usage.cost_usd is not None and cp.usage.cost_usd >= lim.max_usd:
            raise BudgetExceeded("max_usd", f"spent ${cp.usage.cost_usd:.2f}")
        if elapsed >= lim.max_wall_minutes * 60:
            raise BudgetExceeded("max_wall_minutes", f"ran {elapsed / 60:.1f} minutes")

    # --- run ----------------------------------------------------------------------

    def run(self, ctx: ToolContext, *, system: str, task: str, milestone_id: str,
            resume: bool = False) -> RunOutcome:
        cp = self.journal.load_checkpoint(milestone_id) if (resume and self.journal) else None
        if cp is not None and cp.status == "submitted":
            return self._outcome(cp, cp.status)
        answers = dict(ctx.brief.answers) if ctx.brief is not None else {}
        restarted = False
        if cp is None:
            cp = Checkpoint(milestone_id=milestone_id, messages=[Message.user(task)],
                            answered=sorted(answers))
        else:
            restarted = self._resume(cp, answers)
        if cp.submitted:
            ctx.state["submitted"] = cp.submitted
        ctx.state["questions"] = list(cp.questions)
        started = self.clock() - cp.elapsed_seconds
        self.events.emit("run_started", milestone=milestone_id, resumed=cp.step > 0,
                         step=cp.step, model=str(self.adapter.ref))
        repeat, last_sig = (0, None) if restarted else self._recent_repeats(cp.messages)
        unpriced: set[str] = set()
        status, limit = "no_submission", None
        try:
            while True:
                self.check_limits(cp, self.clock() - started)
                resp = self.adapter.complete(system=system, messages=cp.messages,
                                             tools=self.registry.specs(),
                                             max_tokens=self.max_tokens_per_call)
                cp.step += 1
                cp.usage = cp.usage + resp.usage
                cp.messages.append(resp.to_message())
                self._emit_response(cp.step, resp)
                self._record_model(cp, resp)
                if resp.usage.cost_usd is None and resp.usage.total_tokens and resp.model not in unpriced:
                    unpriced.add(resp.model)
                    self.events.emit("cost_unknown", step=cp.step, model=resp.model,
                                     note="no price for this model: its tokens do not count "
                                          "toward max_usd (set AGENTKIT_PRICING_FILE)")
                if resp.stop_reason == "refusal" and not resp.tool_calls:
                    status = "refused"
                    break
                if resp.tool_calls and resp.stop_reason == "max_tokens":
                    # Arguments of a cut-off call may be truncated: never run them.
                    results = [ToolResult(call_id=c.id, name=c.name, content=CUT_OFF_CALL, is_error=True)
                               for c in resp.tool_calls]
                    cp.messages.append(Message.tool(results))
                    self.events.emit("nudge", step=cp.step, reason="cut_off",
                                     skipped_calls=[c.name for c in resp.tool_calls])
                elif resp.tool_calls:
                    self._run_tools(cp, resp.tool_calls, ctx)
                    cp.submitted = ctx.state.get("submitted")
                    cp.questions = list(ctx.state.get("questions", []))
                    if cp.submitted:
                        status = "submitted"
                        break
                    sig = _signature(resp.tool_calls)
                    repeat = repeat + 1 if sig == last_sig else 1
                    last_sig = sig
                    if repeat >= STUCK_STOP_AT:
                        self.events.emit("stuck", step=cp.step, repeats=repeat)
                        status = "stuck"
                        break
                    if repeat == STUCK_NUDGE_AT:
                        self._nudge(cp, NUDGE_STUCK.format(n=repeat), "stuck")
                elif resp.stop_reason == "pause":
                    pass  # re-send the history; the provider resumes the paused turn
                elif resp.stop_reason == "max_tokens":
                    self._nudge(cp, NUDGE_CUT_OFF, "cut_off")
                elif not cp.nudged:
                    cp.nudged = True
                    self._nudge(cp, NUDGE_NO_SUBMIT, "no_submission")
                else:
                    status = "no_submission"
                    break
                self._save(cp, started)
        except BudgetExceeded as exc:
            status, limit = "budget_exceeded", exc.limit
            self.events.emit("budget_exceeded", limit=exc.limit, message=str(exc))
        except ModelError as exc:
            status = "model_error"
            self.events.emit("model_error", provider=exc.provider, retryable=exc.retryable,
                             message=str(exc))
        cp.status = status
        self._save(cp, started)
        outcome = self._outcome(cp, status, limit)
        self.events.emit("run_finished", status=status, steps=cp.step, limit=limit,
                         usage=cp.usage.to_dict())
        return outcome

    # --- helpers --------------------------------------------------------------------

    def _run_tools(self, cp: Checkpoint, calls: list[ToolCall], ctx: ToolContext) -> None:
        """Run a turn's calls in order and append ONE tool message. If a tool
        stops the run (BudgetExceeded), the calls left unrun are answered with
        error results and the message is appended before the exception
        propagates, so every tool call in the history keeps its result."""
        results: list[ToolResult] = []
        for i, call in enumerate(calls):
            self.events.emit("tool_call", step=cp.step, id=call.id, name=call.name,
                             arguments=call.arguments)
            try:
                result = self.registry.execute(call, ctx)
            except BudgetExceeded as exc:
                text = NOT_RUN_BUDGET.format(limit=exc.limit)
                results += [ToolResult(call_id=c.id, name=c.name, content=text, is_error=True)
                            for c in calls[i:]]
                cp.messages.append(Message.tool(results))
                raise
            self.events.emit("tool_result", step=cp.step, id=call.id, name=call.name,
                             is_error=result.is_error, content=result.content[:2000])
            results.append(result)
        cp.messages.append(Message.tool(results))

    def _resume(self, cp: Checkpoint, answers: dict[str, str]) -> bool:
        """Prepare a loaded checkpoint to continue. Returns True when the run
        had stopped (it is restarted with a fresh stuck counter and nudge)."""
        stopped = cp.status != "running"
        discarded = False
        if cp.status == "refused" and cp.messages and cp.messages[-1].role == "assistant":
            # A declined turn's partial output is discarded, not replayed: it
            # was never part of a request, so dropping it edits no history
            # the provider has seen.
            cp.messages.pop()
            discarded = True
        new = {q: a for q, a in answers.items() if q not in cp.answered}
        parts = []
        if stopped:
            parts.append(RESUME_STOPPED.format(status=cp.status))
        if new:
            parts.append("The client answered questions since this milestone started:\n"
                         + wrap_untrusted(json.dumps(new, indent=2, ensure_ascii=False), "client-answers"))
            cp.answered = sorted({*cp.answered, *new})
        if parts:
            cp.messages.append(Message.user("\n\n".join(parts)))
            self.events.emit("nudge", step=cp.step, reason="resume", stopped=cp.status if stopped else None,
                             answers=len(new), discarded_refusal=discarded)
        if stopped:
            cp.status, cp.nudged = "running", False
        return stopped

    def _nudge(self, cp: Checkpoint, text: str, reason: str) -> None:
        cp.messages.append(Message.user(text))
        self.events.emit("nudge", step=cp.step, reason=reason)

    def _save(self, cp: Checkpoint, started: float) -> None:
        cp.elapsed_seconds = self.clock() - started
        if self.journal is not None:
            self.journal.save_checkpoint(cp)

    def _record_model(self, cp: Checkpoint, resp: ModelResponse) -> None:
        """Note the model that produced this turn and journal any switch the
        provider made while serving it."""
        ref = getattr(self.adapter, "ref", None)
        provider = (resp.raw or {}).get("provider") or getattr(ref, "provider", "")
        served = f"{provider}:{resp.model}" if provider else resp.model
        if served not in cp.models:
            cp.models.append(served)
        for switch in resp.fallbacks:
            self.events.emit("model_fallback", step=cp.step, from_model=switch.get("from"),
                             to_model=switch.get("to"), reason="server_side")

    def _emit_response(self, step: int, resp: ModelResponse) -> None:
        self.events.emit("model_response", step=step, model=resp.model, stop_reason=resp.stop_reason,
                         text=resp.text[:2000], tool_calls=[c.name for c in resp.tool_calls],
                         usage=resp.usage.to_dict())

    @staticmethod
    def _recent_repeats(messages: list[Message]) -> tuple[int, str | None]:
        """Rebuild the stuck counter from history (for resume)."""
        repeat, last = 0, None
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                sig = _signature(m.tool_calls)
                repeat = repeat + 1 if sig == last else 1
                last = sig
        return repeat, last

    @staticmethod
    def _outcome(cp: Checkpoint, status: str, limit: str | None = None) -> RunOutcome:
        submitted = cp.submitted or {}
        summary = submitted.get("summary", "")
        if not summary:
            summary = next((m.text for m in reversed(cp.messages) if m.role == "assistant" and m.text), "")
        return RunOutcome(status=status, summary=summary, artifacts=list(submitted.get("artifacts", [])),
                          usage=cp.usage, questions=list(cp.questions), steps=cp.step, limit=limit,
                          models=list(cp.models))


__all__ = ["Limits", "RunOutcome", "Runner", "STUCK_NUDGE_AT", "STUCK_STOP_AT", "metered_tokens"]
