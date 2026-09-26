"""Executor registry (docs/decisions/0001-custody-chain.md §3).

An executor performs an approved action exactly once, inside ``consume()``.
Register one per action kind::

    @executor("milestone.release")
    def release(approval, action) -> ExecutionResult: ...

Work that must see the approval already ``consumed`` and committed (e.g.
minting a mandate from it) registers an after-consume hook instead::

    @after_consume("engagement.fund")
    def mint_root(approval, action) -> None: ...

Hooks run after ``consume()`` commits a successful execution. They may commit
their own writes; an exception is logged and rolled back, never raised.
"""
from __future__ import annotations

import logging
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


log = logging.getLogger("agents_list.approvals")

Executor = Callable[["Approval", dict], ExecutionResult]
EXECUTORS: dict[str, Executor] = {}
Hook = Callable[["Approval", dict], None]
AFTER_CONSUME: dict[str, list[Hook]] = {}


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


def after_consume(kind: str):
    """Decorator registering ``fn`` to run once an approval of ``kind`` has
    been consumed successfully and committed."""
    def register(fn: Hook) -> Hook:
        hooks = AFTER_CONSUME.setdefault(kind, [])
        if fn not in hooks:
            hooks.append(fn)
        return fn
    return register


def run_after_consume(approval: "Approval", action: dict) -> None:
    """Run the hooks for ``approval.kind``. Never raises: a failing hook is
    logged and its uncommitted writes are rolled back."""
    from app.extensions import db
    for fn in AFTER_CONSUME.get(approval.kind, ()):
        try:
            fn(approval, action)
        except Exception:  # noqa: BLE001 - the approval is already consumed
            db.session.rollback()
            log.exception("after-consume hook %s failed for %s", fn.__name__, approval.id)
