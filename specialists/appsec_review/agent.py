"""
specialists/appsec_review/agent.py - the application security review Specialist.

The kit wires in everything beside this module: agent.yaml (milestones,
tools, egress to api.osv.dev only, no shell, the customer-security-lead
gate), tools.py (TOOL_DEFS: scanner, OSV audit, OpenAPI parser, CVSS,
scope guard, SARIF writer) and checks.py (CHECK_DEFS: SARIF structure,
evidence locations, scope, secret reconciliation).

This subclass adds the one rule the generic hooks cannot express: a
security review of someone else's code starts only once the customer has
confirmed it is authorized. The intake answers `authorized` and
`repo_provided` must be exactly true - "false" is an answer the generic
intake check would accept - and run_milestone refuses, before any model
call, when the brief's intake does not confirm authorization.

No finalize hook: the review never changes repo/, so there is no patch to
build, and the human gate comes from the manifest unchanged.
"""
from __future__ import annotations

from typing import Any, Mapping

from agentkit.errors import AgentKitError
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec, MissingInput

# Intake answers that are confirmations: only a literal true lets work start.
CONFIRMATIONS = ("authorized", "repo_provided")


class AppSecReview(Specialist):
    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        flagged = {m.field for m in missing}
        for f in self.manifest.intake:
            if f.field in CONFIRMATIONS and f.field not in flagged and intake.get(f.field) is not True:
                missing.append(MissingInput(field=f.field, blocking=True,
                                            question=f"{f.question} (the review needs a yes)"))
        return missing

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        intake = ctx.brief.intake if ctx.brief is not None else {}
        if intake.get("authorized") is not True:
            raise AgentKitError(f"{self.slug}: the brief's intake must confirm the customer is "
                                "authorized to have this application reviewed (authorized: true)")
        super().prepare(ctx, milestone)
