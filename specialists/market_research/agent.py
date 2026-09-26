"""
specialists/market_research/agent.py - the market-research Specialist.

agent.yaml, TOOL_DEFS (tools.py) and CHECK_DEFS (checks.py) beside this
module do most of the work; the subclass adds what the domain needs:

    extra_checks()     registers every domain check as "automated", so no
                       brief can turn one into a pending rubric or human check
    intake params      the client's intake tier1_share (a share in (0, 1])
                       replaces the threshold of the manifest's own
                       source_tier_mix check, and the intake's competitors
                       become the required rows of matrix_covers_competitors,
                       both when the milestone runs and when it is re-checked
                       with the same brief; a criterion the brief adds keeps
                       its params
    egress             web pages are fetched only from sites someone
                       approved (SourcePlanGate): none in m1-plan (the plan
                       is built offline); afterwards the client's
                       allowed_domains when the intake gives them, else the
                       source_domains of the plan as submitted in m1-plan and
                       approved by the client. Fetched pages and client files
                       share a context, so this bounds where client material
                       could be sent to the sites the client accepted.
    validate_intake()  also reports a malformed tier1_share or allowed_domains
                       (either would stop the run before the first model call)

Nothing else changes: the kit's prepare creates the deliverable folders, the
agent writes every deliverable itself (the domain tools only do the
bookkeeping), and the checks re-derive everything from the ledger.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Mapping

from agentkit.errors import AgentKitError, PolicyViolation
from agentkit.events import EventSink
from agentkit.policy import PolicyGate
from agentkit.security import host_allowed
from agentkit.specialist import RunContext, Specialist
from agentkit.tools import ToolContext
from agentkit.types import Brief, MilestoneSpec, MissingInput
from specialists.market_research import tools
from specialists.market_research.checks import CHECK_DEFS

TIER1_FIELD = "tier1_share"
TIER1_CHECK = "source_tier_mix"
COMPETITORS_FIELD = "competitors"
COMPETITORS_CHECK = "matrix_covers_competitors"
PLAN_MILESTONE = "m1-plan"


def tier1_share(intake: Mapping[str, Any]) -> float | None:
    """The client's minimum share of tier-1 sources, or None when the intake
    leaves it out; AgentKitError unless it is a number in (0, 1]."""
    value = intake.get(TIER1_FIELD)
    if value is None or value == "":
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 < value <= 1):
        raise AgentKitError(f"intake {TIER1_FIELD!r} must be a number between 0 and 1 "
                            f"(e.g. 0.6), got {value!r}")
    return float(value)


def named_competitors(intake: Mapping[str, Any]) -> list[str]:
    """Competitors the client says must be covered: a list of names, or one
    string separated by commas, semicolons or new lines."""
    value = intake.get(COMPETITORS_FIELD)
    if isinstance(value, str):
        items = re.split(r"[,;\n]", value)
    elif isinstance(value, list):
        items = [v for v in value if isinstance(v, str)]
    else:
        return []
    return [name for name in (tools.normalize_ws(i) for i in items) if name]


class SourcePlanGate(PolicyGate):
    """The kit's PolicyGate plus one rule: a URL's host must also match
    `hosts` (None = no extra rule; [] = no web access at all)."""

    hosts: list[str] | None = None
    reason: str = ""

    @classmethod
    def around(cls, gate: PolicyGate, hosts: list[str] | None, reason: str) -> "SourcePlanGate":
        narrowed = cls.__new__(cls)
        narrowed.__dict__.update(vars(gate))
        narrowed.hosts, narrowed.reason = hosts, reason
        return narrowed

    def check_url(self, url: str) -> str:
        host = super().check_url(url)
        if self.hosts is not None and not host_allowed(host, self.hosts):
            raise PolicyViolation(f"host {host!r} {self.reason}")
        return host


def approved_hosts(gate: PolicyGate, milestone_id: str, workspace: Path) -> tuple[list[str] | None, str]:
    """The extra host rules for a milestone's fetches and why a host is refused."""
    if milestone_id == PLAN_MILESTONE:
        return [], ("is not reachable in m1-plan: the plan is built from the brief and inputs/, and "
                    "the client approves its source_domains before any page is fetched")
    if gate.egress_narrow is not None:
        return None, ""       # the client's allowed_domains already bound every fetch
    tree, problem = tools.load_plan(workspace)
    domains = tools.plan_domains(tree) if not problem else {}
    if not domains:
        return [], f"is not reachable: {problem or 'the approved plan names no source_domains'}"
    return sorted(domains), ("is not in the approved plan's source_domains; the client approves "
                             "sites in m1-plan or through the allowed_domains intake field")


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
        with the client's tier-1 threshold and named competitors applied to
        the manifest's own criteria."""
        m = super()._milestone(brief, milestone)
        base = self.manifest.milestone(m.id)
        if base is None:
            return m
        overrides = {TIER1_CHECK: ("min_tier1_share", tier1_share(brief.intake)),
                     COMPETITORS_CHECK: ("required", named_competitors(brief.intake) or None)}
        for check, (key, value) in overrides.items():
            own = [a.params for a in base.acceptance if a.check == check]
            for a in m.acceptance:
                if value is not None and a.check == check and a.params in own:
                    a.params = {**a.params, key: value}
        return m

    def _tool_context(self, ctx: RunContext, milestone: MilestoneSpec, events: EventSink) -> ToolContext:
        """The kit's ToolContext with the source-plan egress rule. The plan is
        read once, before the model's first step, so a plan the model
        rewrites mid-run never widens its own access."""
        tool_ctx = super()._tool_context(ctx, milestone, events)
        hosts, reason = approved_hosts(tool_ctx.policy, milestone.id, tool_ctx.workspace)
        tool_ctx.policy = SourcePlanGate.around(tool_ctx.policy, hosts, reason)
        return tool_ctx
