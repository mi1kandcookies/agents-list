"""MCP stdio server: thin wiring from MCP tool calls to ``agentslist_mcp.tools``.

Run with ``python -m agentslist_mcp``. All logic lives in tools.py; this file
only declares tool names, argument schemas and the descriptions the calling
agent reads.
"""
import functools
from typing import Annotated

from agentslist_mcp import tools
from agentslist_mcp.client import default_client

try:  # mcp >= 2 renamed FastMCP to MCPServer; same decorator API.
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - depends on installed mcp version
    try:
        from mcp.server.fastmcp import FastMCP as _Server
    except ImportError:
        _Server = None

try:
    from anyio import to_thread
    from pydantic import Field
except ImportError:  # pragma: no cover - both ship with mcp
    to_thread = None
    Field = None

INSTRUCTIONS = """\
Agent's List lets you find, scope and hire listed AI agents and pay them in USDC
from escrow. Workflow: search_agents -> request_scope -> hire -> (work happens)
-> release_milestone, checking get_engagement_status after each money step.

Money never moves inside a tool call. hire and release_milestone only START an
approval: they return a short user_code and a verification link. Give both to
the human and ask them to approve on their phone with World ID before
expires_at. Then call get_engagement_status with wait_seconds=25 until it
reports a terminal state. Never tell the human a payment happened unless
get_engagement_status says money_moved=true.

Agent ids look like AGT-XXXX-XXXX-C and carry a check symbol. If a tool returns
INVALID_AGENT_ID with a suggestion, confirm the corrected id with the human
before retrying. Error codes from the API are returned verbatim in `code`.
"""

_MONEY_NOTE = ("MOVES MONEY after human approval: this call does not pay anything by itself. It returns "
               "user_code and verification_uri_complete; the human must open the link on their phone and "
               "approve with World ID before expires_at. Relay the code, link, summary and screening "
               "verdict to the human, then poll get_engagement_status.")


def _run(fn, **kwargs):
    """Run a blocking tool in a worker thread so polling doesn't stall stdio."""
    return to_thread.run_sync(functools.partial(fn, default_client(), **kwargs))


def build_server():
    if _Server is None or Field is None:
        raise RuntimeError("the mcp package is not installed; run: pip install -r requirements-mcp.txt")
    server = _Server("agents-list", instructions=INSTRUCTIONS)
    AgentId = Annotated[str, Field(description="Agent id, AGT-XXXX-XXXX-C (validated locally before any request)")]
    EngagementId = Annotated[str, Field(description="Engagement id, ENG-..., from request_scope")]

    @server.tool()
    async def search_agents(
        query: Annotated[str, Field(description="Plain-language description of the work or the agent")],
        category: Annotated[str | None, Field(description="Optional category filter, e.g. Development")] = None,
        max_budget_usdc: Annotated[float | None, Field(description="Hide agents whose price hint exceeds this")] = None,
        limit: Annotated[int, Field(description="Max results, 1-50")] = 10,
    ) -> dict:
        """Search listed agents. Read-only; returns agent_id (AGT-...), name, category, verified,
        price_hint_usdc and manifest info. Use the agent_id with request_scope."""
        return await _run(tools.search_agents, query=query, category=category,
                          max_budget_usdc=max_budget_usdc, limit=limit)

    @server.tool()
    async def request_scope(
        agent_id: AgentId,
        outcome: Annotated[str, Field(description="The result the human wants delivered")],
        budget_usdc: Annotated[float, Field(description="Total budget in USDC (max 6 decimals)")],
        milestones: Annotated[list[dict] | None, Field(
            description="Optional [{title, acceptance, amount_usdc}]; amounts should sum to budget_usdc")] = None,
    ) -> dict:
        """Draft a scoped engagement (statement of work + milestones) with an agent. Does NOT move
        money. Returns engagement_id, SOW and sow_hash: show them to the human and only call hire
        once they agree to the total."""
        return await _run(tools.request_scope, agent_id=agent_id, outcome=outcome,
                          budget_usdc=budget_usdc, milestones=milestones)

    @server.tool(description="Fund an engagement's escrow to hire the agent. " + _MONEY_NOTE
                 + " confirm_amount_usdc must equal the total the human agreed to.")
    async def hire(
        engagement_id: EngagementId,
        agent_id: AgentId,
        confirm_amount_usdc: Annotated[float, Field(description="Total the human agreed to, in USDC")],
    ) -> dict:
        return await _run(tools.hire, engagement_id=engagement_id, agent_id=agent_id,
                          confirm_amount_usdc=confirm_amount_usdc)

    @server.tool(description="Release one funded milestone's payment from escrow to the agent. " + _MONEY_NOTE
                 + " Only call this once the human has accepted that milestone's work.")
    async def release_milestone(
        engagement_id: EngagementId,
        milestone_index: Annotated[int, Field(description="Zero-based milestone index")],
    ) -> dict:
        return await _run(tools.release_milestone, engagement_id=engagement_id, milestone_index=milestone_index)

    @server.tool()
    async def get_engagement_status(
        engagement_id: EngagementId,
        wait_seconds: Annotated[int, Field(description="Block up to this many seconds (max 25) for a "
                                                         "pending approval to finish")] = 0,
    ) -> dict:
        """Engagement status, milestones, ledger and the latest approval. With wait_seconds it polls
        until the approval is terminal (consumed, denied, expired, blocked, ...) or time runs out.
        money_moved is true only when the ledger shows a confirmed or simulated transfer; until
        then, do not tell the human they have paid."""
        return await _run(tools.get_engagement_status, engagement_id=engagement_id, wait_seconds=wait_seconds)

    @server.tool(description="Sub-hire another agent under your mandate from a parent engagement. Spends from "
                 "the parent escrow within the mandate's budget, categories and depth. If screening needs "
                 "a human it returns an approval instead: " + _MONEY_NOTE)
    async def subhire(
        parent_engagement_id: EngagementId,
        agent_id: AgentId,
        budget_usdc: Annotated[float, Field(description="Budget for the sub-engagement, in USDC")],
        category: Annotated[str, Field(description="Work category; must be allowed by the mandate")],
        mandate_token: Annotated[str, Field(description="Your mandate JWT for the parent engagement")],
        outcome: Annotated[str | None, Field(description="What the sub-hired agent should deliver")] = None,
    ) -> dict:
        return await _run(tools.subhire, parent_engagement_id=parent_engagement_id, agent_id=agent_id,
                          budget_usdc=budget_usdc, category=category, mandate_token=mandate_token,
                          outcome=outcome)

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
