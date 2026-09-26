"""
specialists/bookkeeping_close/agent.py - the month-end close specialist.

The kit wires the domain pack beside this file: agent.yaml (manifest),
tools.py (TOOL_DEFS, wrapped by tools_from_defs, which hands each tool the
PolicyGate-checked resolve_path) and checks.py (CHECK_DEFS, wrapped by
CheckRegistry.add_defs). This subclass adds what a close needs around a run:

    validate_intake  a period that is not YYYY-MM is a blocking gap (every
                     schedule, cut-off and check keys off it); a stated
                     ending balance that is not an amount is flagged
    prepare          writes inputs/close_parameters.json from the intake when
                     the client did not supply one, so the checks recompute
                     against the client's period, cash account, stated
                     statement balance and flux thresholds instead of
                     falling back to figures the agent reported

Nothing here posts, files or relaxes the human gate: human_review comes from
the manifest (accountant or CPA), and every journal entry stays a draft.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from agentkit.errors import ToolError
from agentkit.journal import write_json_atomic
from agentkit.policy import jail_path
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec, MissingInput
from specialists.bookkeeping_close.checks import CLOSE_PARAMETERS
from specialists.bookkeeping_close.tools import money

_PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
# Intake answers copied into close_parameters.json as given.
_PARAMETER_FIELDS = ("entity_name", "period", "cash_account", "statement_ending_balance")
# A number, optionally with $ and thousands separators, optionally a percentage.
_THRESHOLD = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(%|percent\b)?", re.I)


def materiality_thresholds(value: Any) -> dict[str, str]:
    """Flux thresholds from the intake's free-text materiality answer, e.g.
    "1000 and 10%" or "$2,500 / 5 percent": the first plain amount is the
    absolute threshold, the first percentage the relative one. Anything
    unreadable is left out, so the default thresholds apply."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return {}
    text = str(value)
    out: dict[str, str] = {}
    for m in _THRESHOLD.finditer(text):
        if text[m.end(1):m.end(1) + 1].isalpha():
            continue                        # "1k", "10x": not a figure we can use
        key = "flux_threshold_pct" if m.group(2) else "flux_threshold_abs"
        out.setdefault(key, m.group(1).replace(",", ""))
    return out


def close_parameters(intake: Mapping[str, Any]) -> dict[str, str]:
    """The close_parameters.json the checks read, built from the intake."""
    params = {k: str(intake[k]).strip() for k in _PARAMETER_FIELDS
              if intake.get(k) is not None and str(intake[k]).strip()}
    params.update(materiality_thresholds(intake.get("materiality")))
    return params


class BookkeepingClose(Specialist):
    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        period = intake.get("period")
        if period not in (None, "") and not _PERIOD.fullmatch(str(period).strip()):
            missing.append(MissingInput(
                field="period", blocking=True,
                question=f"Which month are we closing? Please give it as YYYY-MM (got {period!r})."))
        stated = intake.get("statement_ending_balance")
        if stated not in (None, ""):
            try:
                money(stated)
            except ToolError:
                missing.append(MissingInput(
                    field="statement_ending_balance", blocking=False,
                    question="What ending balance is printed on the bank statement? Please give "
                             f"it as an amount such as 36945.58 (got {stated!r})."))
        return missing

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        super().prepare(ctx, milestone)
        target = jail_path(ctx.workspace, CLOSE_PARAMETERS)
        if target.exists():
            return                          # the client's own file wins
        params = close_parameters(ctx.brief.intake if ctx.brief else {})
        if params:
            write_json_atomic(target, params)
            ctx.events.emit("close_parameters", path=CLOSE_PARAMETERS, fields=sorted(params))
