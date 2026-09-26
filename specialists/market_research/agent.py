"""
specialists/market_research/agent.py - the market-research Specialist.

agent.yaml, TOOL_DEFS (tools.py) and CHECK_DEFS (checks.py) beside this
module do most of the work; the subclass adds what the domain needs:

    extra_checks()     registers every domain check as "automated", so no
                       brief can turn one into a pending rubric or human check
    tier-1 threshold   the client's intake tier1_share (a share in (0, 1])
                       replaces the threshold of the manifest's own
                       source_tier_mix check, both when the milestone runs
                       and when it is re-checked with the same brief; a
                       source_tier_mix criterion the brief adds keeps its params
    validate_intake()  also reports a malformed tier1_share or allowed_domains
                       (either would stop the run before the first model call)

Nothing else changes: the kit's prepare creates the deliverable folders, the
agent writes every deliverable itself (the domain tools only do the
bookkeeping), and the checks re-derive everything from the ledger.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from agentkit.errors import AgentKitError
from agentkit.specialist import Specialist
from agentkit.types import Brief, MilestoneSpec, MissingInput
from specialists.market_research.checks import CHECK_DEFS

TIER1_FIELD = "tier1_share"
TIER1_CHECK = "source_tier_mix"


def tier1_share(intake: Mapping[str, Any]) -> float | None:
    """The client's minimum share of tier-1 claims, or None when the intake
    leaves it out; AgentKitError unless it is a number in (0, 1]."""
    value = intake.get(TIER1_FIELD)
    if value is None or value == "":
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 < value <= 1):
        raise AgentKitError(f"intake {TIER1_FIELD!r} must be a number between 0 and 1 "
                            f"(e.g. 0.6), got {value!r}")
    return float(value)


class MarketResearch(Specialist):
    def extra_checks(self) -> list[dict[str, Any]]:
        return [{"name": name, "function": fn, "kind": "automated"} for name, fn in CHECK_DEFS.items()]

    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        try:
            tier1_share(intake)
        except AgentKitError as exc:
            missing.append(MissingInput(field=TIER1_FIELD, question=str(exc), blocking=True))
        try:  # the same validation the run applies to allowed_domains
            self.policy(Brief(engagement_id="", specialist=self.slug, objective="", intake=dict(intake)))
        except AgentKitError as exc:
            missing.append(MissingInput(field=self.manifest.egress.intake_field, question=str(exc),
                                        blocking=True))
        return missing

    def _milestone(self, brief: Brief, milestone: MilestoneSpec | str) -> MilestoneSpec:
        """The kit's milestone resolution (used by run_milestone and check),
        with the client's tier-1 threshold applied."""
        m = super()._milestone(brief, milestone)
        share = tier1_share(brief.intake)
        base = self.manifest.milestone(m.id)
        if share is None or base is None:
            return m
        own = [a.params for a in base.acceptance if a.check == TIER1_CHECK]
        for a in m.acceptance:
            if a.check == TIER1_CHECK and a.params in own:
                a.params = {**a.params, "min_tier1_share": share}
        return m
