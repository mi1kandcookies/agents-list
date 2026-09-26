"""
agentkit/checks - acceptance checks and the registry that runs them.

A check is `fn(workspace: Path, params: dict, ctx: CheckContext) -> CheckResult`.
Milestones list their checks in agent.yaml (AcceptanceCriterion); run_checks()
runs each one and never lets a crashing check pass. Kinds:

    automated  deterministic; must pass for status ready_for_review
    rubric     grader model scores against a rubric; None when no grader
    human      always pending (None); a person signs off on the platform

A check registered with a kind (every builtin, and domain checks listed as
{"name", "function", "kind"}) always reports that kind; the criterion's kind
only applies to checks registered without one.

Specialists add domain checks as `fn(workspace, params, *, run=None) ->
{"passed", "details", "score"}` in CHECK_DEFS; CheckRegistry.add_defs wraps them.
A score is a fraction (normally 0-1); run_check keeps it to 4 decimals (basis
points, as the evidence hashes it) and drops anything that is not a finite
number.
"""
from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from agentkit.events import EventSink, NullSink
from agentkit.ledger import Ledger
from agentkit.types import AcceptanceCriterion, CheckKind, CheckResult, MilestoneSpec

CHECK_KINDS = ("automated", "rubric", "human")


@dataclass
class CheckContext:
    grader: Any = None                      # ModelAdapter for rubric checks, or None
    run: Callable[..., Any] | None = None  # ToolContext.run for command checks
    manifest: Any = None                   # Manifest (rubric paths, disclaimer)
    milestone: MilestoneSpec | None = None
    events: EventSink = field(default_factory=NullSink)
    ledger: Ledger | None = None


CheckFn = Callable[[Path, dict, CheckContext], CheckResult]


def _from_domain(name: str, fn: Callable[..., Any]) -> CheckFn:
    params_sig = inspect.signature(fn).parameters
    takes_run = "run" in params_sig or any(p.kind is p.VAR_KEYWORD for p in params_sig.values())

    def check(workspace: Path, params: dict, ctx: CheckContext) -> CheckResult:
        out = fn(workspace, params, run=ctx.run) if takes_run else fn(workspace, params)
        if isinstance(out, CheckResult):
            return out
        passed = out.get("passed")
        return CheckResult(check=name, passed=None if passed is None else bool(passed),
                           details=str(out.get("details", "")), score=out.get("score"))

    return check


class CheckRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, CheckFn] = {}
        self._kinds: dict[str, CheckKind] = {}

    def register(self, name: str, fn: CheckFn, *, kind: CheckKind | None = None,
                 replace: bool = False) -> None:
        if name in self._checks and not replace:
            raise ValueError(f"duplicate check {name!r}")
        if kind is not None and kind not in CHECK_KINDS:
            raise ValueError(f"check {name!r}: kind must be one of {', '.join(CHECK_KINDS)}")
        self._checks[name] = fn
        if kind is not None:
            self._kinds[name] = kind
        else:
            self._kinds.pop(name, None)

    def add_defs(self, defs: Mapping[str, Callable[..., Any]] | Iterable[Any]) -> None:
        """Register a specialist's CHECK_DEFS (a name -> fn mapping, or a list
        of {"name", "function", "kind"?} dicts)."""
        if isinstance(defs, Mapping):
            items = [(name, fn, None) for name, fn in defs.items()]
        else:
            items = [(d["name"], d.get("function") or d.get("fn"), d.get("kind")) for d in defs]
        for name, fn, kind in items:
            if fn is None:
                raise ValueError(f"check def {name!r} has no function")
            self.register(name, _from_domain(name, fn), kind=kind)

    def get(self, name: str) -> CheckFn | None:
        return self._checks.get(name)

    def kind(self, name: str) -> CheckKind | None:
        """The kind the check was registered with, or None (criterion decides)."""
        return self._kinds.get(name)

    def names(self) -> list[str]:
        return list(self._checks)

    def __contains__(self, name: str) -> bool:
        return name in self._checks


def _score(value: Any) -> float | None:
    """A score to basis-point precision; None unless it is a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 4)


def run_check(criterion: AcceptanceCriterion, workspace: Path, registry: CheckRegistry,
              ctx: CheckContext) -> CheckResult:
    fn = registry.get(criterion.check)
    if fn is None:
        result = CheckResult(check=criterion.check, passed=False, details="unknown check")
    else:
        try:
            result = fn(Path(workspace), dict(criterion.params), ctx)
        except Exception as exc:  # a crashing check fails, never passes
            result = CheckResult(check=criterion.check, passed=False,
                                 details=f"check error: {type(exc).__name__}: {exc}")
    result.check = criterion.check
    result.kind = registry.kind(criterion.check) or criterion.kind
    result.score = _score(result.score)
    if result.kind == "human":
        result.passed = None       # only a person can sign off
    if not result.details and criterion.description:
        result.details = criterion.description
    ctx.events.emit("check_result", **result.to_dict())
    return result


def run_checks(criteria: Iterable[AcceptanceCriterion], workspace: Path, registry: CheckRegistry,
               ctx: CheckContext) -> list[CheckResult]:
    return [run_check(c, workspace, registry, ctx) for c in criteria]


def default_registry() -> CheckRegistry:
    from agentkit.checks.builtin import BUILTIN_CHECKS, BUILTIN_KINDS

    reg = CheckRegistry()
    for name, fn in BUILTIN_CHECKS.items():
        reg.register(name, fn, kind=BUILTIN_KINDS[name])
    return reg


__all__ = ["CHECK_KINDS", "CheckContext", "CheckFn", "CheckRegistry", "default_registry", "run_check",
           "run_checks"]
