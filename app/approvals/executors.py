"""Executor registry (docs/decisions/0001-custody-chain.md §3).

An executor performs an approved action exactly once, inside ``consume()``.
Register one per action kind::

    @executor("milestone.release")
    def release(approval, action) -> ExecutionResult: ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.models import Approval


@dataclass
class ExecutionResult:
    ok: bool
    summary: str
    ledger_ids: list[str] = field(default_factory=list)
    redirect: str | None = None


Executor = Callable[["Approval", dict], ExecutionResult]
EXECUTORS: dict[str, Executor] = {}


def executor(kind: str):
    """Decorator registering ``fn`` as the executor for ``kind``."""
    def register(fn: Executor) -> Executor:
        existing = EXECUTORS.get(kind)
        if existing is not None and existing is not fn:
            raise ValueError(f"executor for {kind!r} already registered")
        EXECUTORS[kind] = fn
        return fn
    return register


def get_executor(kind: str) -> Executor:
    try:
        return EXECUTORS[kind]
    except KeyError:
        raise LookupError(f"no executor registered for {kind!r}") from None
