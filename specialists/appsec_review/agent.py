"""
specialists/appsec_review/agent.py - the application security review Specialist.

The kit wires in everything beside this module: agent.yaml (milestones,
tools, egress to api.osv.dev only, no shell, the customer-security-lead
gate), tools.py (TOOL_DEFS: scanner, OSV audit, OpenAPI parser, CVSS,
scope guard, SARIF writer) and checks.py (CHECK_DEFS: SARIF structure,
evidence locations, CVSS consistency, scope, secret reconciliation and
leakage, advisory ids, attack-surface coverage).

This subclass adds the rules the generic hooks cannot express:

- A security review of someone else's code starts only once the customer
  has confirmed it is authorized. The intake answers `authorized` and
  `repo_provided` must be exactly true - "false" is an answer the generic
  intake check would accept - and run_milestone refuses, before any model
  call, when the brief's intake does not confirm authorization.
- The dependency audit sends package names and versions to a third party
  (OSV), so the customer's intake governs it, not the model: with
  `osv_lookup: false` audit_dependencies is denied by policy, and names in
  `internal_packages` are always excluded from what it sends.

No finalize hook: the review never changes repo/, so there is no patch to
build, and the human gate comes from the manifest unchanged.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

from agentkit.errors import AgentKitError, PolicyViolation
from agentkit.specialist import Specialist
from agentkit.tools import Tool, ToolContext
from agentkit.types import MilestoneSpec, MissingInput

# Intake answers that are confirmations: only a literal true lets work start.
CONFIRMATIONS = ("authorized", "repo_provided")


def _intake(ctx: ToolContext) -> Mapping[str, Any]:
    return ctx.brief.intake if ctx.brief is not None else {}


def _internal_packages(intake: Mapping[str, Any]) -> list[str] | None:
    """The intake's internal package list, or None when it is malformed."""
    value = intake.get("internal_packages")
    if value is None or value == "":
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


def _customer_governed_audit(tool: Tool) -> Tool:
    """audit_dependencies under the customer's OSV consent and exclusions."""
    handler = tool.handler

    def guarded(args: dict, ctx: ToolContext) -> Any:
        intake = _intake(ctx)
        if intake.get("osv_lookup") is False:
            raise PolicyViolation("the customer opted out of OSV lookups (intake osv_lookup: false); "
                                  "review the pinned versions from the manifest and state in the "
                                  "report that no vulnerability database was queried")
        internal = _internal_packages(intake)
        if internal is None:
            raise PolicyViolation("intake internal_packages must be a list of names or prefixes")
        if internal:
            given = args.get("exclude") or []
            args = {**args, "exclude": sorted({*map(str, given), *internal})}
        return handler(args, ctx)

    return dataclasses.replace(tool, handler=guarded)


class AppSecReview(Specialist):
    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        flagged = {m.field for m in missing}
        for f in self.manifest.intake:
            if f.field in CONFIRMATIONS and f.field not in flagged and intake.get(f.field) is not True:
                missing.append(MissingInput(field=f.field, blocking=True,
                                            question=f"{f.question} (the review needs a yes)"))
        if intake.get("osv_lookup") is not None and not isinstance(intake.get("osv_lookup"), bool):
            missing.append(MissingInput(field="osv_lookup", blocking=True,
                                        question="May the dependency audit query OSV? Answer true or false."))
        if _internal_packages(intake) is None:
            missing.append(MissingInput(field="internal_packages", blocking=True,
                                        question="List internal package names or prefixes as a list of strings."))
        return missing

    def extra_tools(self) -> list[Tool]:
        return [_customer_governed_audit(t) if t.name == "audit_dependencies" else t
                for t in super().extra_tools()]

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        if _intake(ctx).get("authorized") is not True:
            raise AgentKitError(f"{self.slug}: the brief's intake must confirm the customer is "
                                "authorized to have this application reviewed (authorized: true)")
        super().prepare(ctx, milestone)
