"""
specialists/financial_research/agent.py - the financial-research Specialist.

The manifest (agent.yaml), the domain tools (tools.py, TOOL_DEFS) and the
acceptance checks (checks.py, CHECK_DEFS) beside this module are wired by
the kit. What this subclass adds:

    - SEC fair access: the EDGAR tools send the User-Agent the client
      declared in intake field sec_user_agent (organization and contact
      email). The model's own user_agent argument is only used when the
      intake has none (e.g. the client answered an ask_client question);
      the harness's SEC_USER_AGENT is the last resort.
    - validate_intake also reports a sec_user_agent without a contact email
      as missing: SEC requests are refused without one.
    - propose_milestones scopes the coverage checks to the brief: the
      companies the client named by CIK are passed as `ciks` to
      comps_tie_to_xbrl and red_flag_checklist, so a company dropped from
      the agent's own tables still fails acceptance.

There is no licensed-reviewer gate (human_gate.required is false in the
manifest); the disclaimer and the no-recommendation checks apply to every
memo, and nothing here changes the human review the manifest declares.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Mapping

from agentkit.specialist import Specialist
from agentkit.tools import Tool, ToolContext
from agentkit.types import Brief, MilestoneSpec, MissingInput

USER_AGENT_FIELD = "sec_user_agent"
# Domain tools that call SEC EDGAR through the kit's egress-checked fetch.
EDGAR_TOOLS = ("sec_company_lookup", "edgar_submissions", "edgar_companyfacts",
               "edgar_filing_text")
# Acceptance checks that take the companies in scope as params.ciks.
SCOPED_CHECKS = ("comps_tie_to_xbrl", "red_flag_checklist")
_CIK_RE = re.compile(r"(?i)^\s*(?:CIK\s*)?0*(\d{1,10})\s*$")


def valid_user_agent(value: Any) -> bool:
    """SEC fair access wants a requester name and a contact email."""
    return isinstance(value, str) and "@" in value and len(value.strip()) > 3


def declared_user_agent(brief: Brief | None, env: Mapping[str, str] | None = None,
                        requested: Any = None) -> str | None:
    """The User-Agent for SEC requests: the client's intake value, else the
    model's argument, else SEC_USER_AGENT from the run's environment."""
    intake = brief.intake.get(USER_AGENT_FIELD) if brief is not None else None
    for candidate in (intake, requested, (env or {}).get("SEC_USER_AGENT")):
        if valid_user_agent(candidate):
            return candidate.strip()
    return None


def intake_ciks(intake: Mapping[str, Any]) -> list[str]:
    """The CIKs the client named in intake `companies` (a list or a comma
    list; entries that are tickers or names are left for the agent to
    resolve with sec_company_lookup)."""
    companies = intake.get("companies")
    if isinstance(companies, str):
        items: list[Any] = re.split(r"[,;\n]", companies)
    elif isinstance(companies, list):
        items = companies
    else:
        return []
    found = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("cik", "")
        m = _CIK_RE.match(str(item)) if isinstance(item, (str, int)) and not isinstance(item, bool) else None
        if m and int(m.group(1)) > 0:
            found.add(str(int(m.group(1))))
    return sorted(found, key=int)


def with_declared_user_agent(tool: Tool) -> Tool:
    """The same tool, with the User-Agent chosen by declared_user_agent."""
    inner = tool.handler

    def handler(args: dict, ctx: ToolContext) -> Any:
        ua = declared_user_agent(ctx.brief, ctx.env, args.get("user_agent"))
        if ua:
            args = {**args, "user_agent": ua}
        return inner(args, ctx)

    return dataclasses.replace(tool, handler=handler)


class FinancialResearch(Specialist):
    def propose_milestones(self, intake: Mapping[str, Any] | None = None) -> list[MilestoneSpec]:
        milestones = super().propose_milestones(intake)
        ciks = intake_ciks(intake or {})
        if ciks:
            for m in milestones:
                for a in m.acceptance:
                    if a.check in SCOPED_CHECKS and "ciks" not in a.params:
                        a.params = {**a.params, "ciks": ciks}
        return milestones

    def extra_tools(self) -> list[Tool]:
        return [with_declared_user_agent(t) if t.name in EDGAR_TOOLS else t
                for t in super().extra_tools()]

    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        value = intake.get(USER_AGENT_FIELD)
        if value and not valid_user_agent(value) and all(m.field != USER_AGENT_FIELD for m in missing):
            field = next((f for f in self.manifest.intake if f.field == USER_AGENT_FIELD), None)
            question = field.question if field else "Which organization and contact email should " \
                                                     "SEC requests declare as their User-Agent?"
            missing.append(MissingInput(field=USER_AGENT_FIELD, blocking=field.required if field else True,
                                        question=f"{question} (The value given has no contact email.)"))
        return missing


__all__ = ["EDGAR_TOOLS", "FinancialResearch", "SCOPED_CHECKS", "declared_user_agent",
           "intake_ciks", "valid_user_agent", "with_declared_user_agent"]
