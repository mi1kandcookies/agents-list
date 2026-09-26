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

Two more per-kind hooks:

    @before_consume("engagement.fund")      # raise ApprovalError to block
    def still_hireable(approval, action) -> None: ...

    @on_terminal("subhire.fund")            # denied/expired/cancelled/rejected/blocked
    def drop_child(approval, action) -> None: ...

``before_consume`` checks run inside ``consume()`` with its other re-checks,
before ``consumed_at`` is set; an ``ApprovalError`` ends the approval
``blocked`` with that code. ``on_terminal`` hooks run inside the transaction
that moves an approval to a terminal state other than consumed / failed, in a
savepoint: they must not commit, and an exception is logged and rolled back
to the savepoint, never raised.
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
BEFORE_CONSUME: dict[str, list[Hook]] = {}
ON_TERMINAL: dict[str, list[Hook]] = {}
# Terminal states that end an approval without it having executed or failed.
UNEXECUTED_TERMINAL = frozenset({"denied", "expired", "cancelled", "rejected", "blocked"})


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


def _registrar(table: dict[str, list[Hook]], kind: str):
    def register(fn: Hook) -> Hook:
        hooks = table.setdefault(kind, [])
        if fn not in hooks:
            hooks.append(fn)
        return fn
    return register


def before_consume(kind: str):
    """Decorator registering a check run inside ``consume()`` just before an
    approval of ``kind`` executes; raising ``ApprovalError`` blocks it."""
    return _registrar(BEFORE_CONSUME, kind)


def run_before_consume(approval: "Approval", action: dict) -> None:
    """Run the checks for ``approval.kind``; an ApprovalError propagates."""
    for fn in BEFORE_CONSUME.get(approval.kind, ()):
        fn(approval, action)


def on_terminal(kind: str):
    """Decorator registering ``fn`` to run when an approval of ``kind`` ends
    in one of ``UNEXECUTED_TERMINAL`` (see the module docstring)."""
    return _registrar(ON_TERMINAL, kind)


def run_on_terminal(approval: "Approval", action: dict) -> None:
    """Run the terminal hooks for ``approval.kind``, each in a savepoint.
    Never raises."""
    from app.extensions import db
    for fn in ON_TERMINAL.get(approval.kind, ()):
        try:
            with db.session.begin_nested():
                fn(approval, action)
        except Exception:  # noqa: BLE001 - the approval's own transition stands
            log.exception("terminal hook %s failed for %s", fn.__name__, approval.id)
