"""
specialists/data_analyst/agent.py - the data-analyst Specialist.

The kit's Specialist base wires everything beside this module: agent.yaml,
the domain tools in tools.py (TOOL_DEFS) and the acceptance checks in
checks.py (CHECK_DEFS). The domain adds one hook:

    finalize   adds the milestone's saved queries (queries/*.sql) and their
               results (results/*.csv) to the submitted artifacts. The
               acceptance checks re-run exactly those files, so they belong
               in the evidence hash with the prose, whether or not the model
               listed them in submit_milestone.

There is no human gate (manifest human_gate.required is false); the M2
metric definitions carry a human_signoff check for the customer's metric
owner, which stays pending until that person signs off on the platform.
"""
from __future__ import annotations

from agentkit.errors import PolicyViolation
from agentkit.loop import RunOutcome
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec

# Folder under deliverables/<milestone>/ -> the evidence files save_query writes there.
EVIDENCE_FILES = {"queries": ".sql", "results": ".csv"}


class DataAnalyst(Specialist):

    def finalize(self, ctx: ToolContext, milestone: MilestoneSpec, outcome: RunOutcome) -> RunOutcome:
        outcome = super().finalize(ctx, milestone, outcome)
        evidence = []
        for folder, suffix in EVIDENCE_FILES.items():
            try:
                base = ctx.path(f"deliverables/{milestone.id}/{folder}")
            except PolicyViolation:
                continue
            if base.is_dir():
                evidence += sorted(ctx.rel(p) for p in base.iterdir()
                                   if p.suffix.lower() == suffix and p.is_file() and not p.is_symlink())
        outcome.artifacts = list(dict.fromkeys([*outcome.artifacts, *evidence]))
        return outcome
