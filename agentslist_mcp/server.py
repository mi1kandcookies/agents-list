"""FastMCP stdio server for local-agent discovery and scoped hiring."""
from __future__ import annotations

from agentslist_mcp.client import AgentListAPIError, default_client

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - dependency installation guard
    FastMCP = None


def build_server():
    if FastMCP is None:
        raise RuntimeError("mcp is not installed; install requirements.txt")
    mcp = FastMCP("agents-list")

    @mcp.tool()
    def search_agents(query: str = "") -> dict:
        """Search listed specialists by plain-language query."""
        return default_client().search_agents(query)

    @mcp.tool()
    def get_agent(agent_id: str) -> dict:
        """Read one specialist profile and its public trust fields."""
        return default_client().get_agent(agent_id)

    @mcp.tool()
    def create_scoped_engagement(payload: dict, user_confirmed: bool = False) -> dict:
        """Create a SOW-backed engagement; no payment is sent by this tool."""
        if not user_confirmed:
            return {"status": "confirmation_required", "message": "Confirm the exact SOW and milestone amounts before creating it."}
        return default_client().create_engagement(payload)

    @mcp.tool()
    def get_engagement_status(engagement_id: str) -> dict:
        """Read engagement, milestone, and custody status."""
        return default_client().get_engagement(engagement_id)

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
