"""
agentkit/tools - built-in tools and the Tool/ToolContext/ToolRegistry types.

builtin_registry() holds every kit tool; a specialist's registry is the
subset its manifest lists plus its own TOOL_DEFS.
"""
from __future__ import annotations

from agentkit.tools import documents, ledger_tools, platform, shell, web, workspace
from agentkit.tools.base import (CommandResult, FetchResult, Tool, ToolContext, ToolRegistry,
                                 tools_from_defs, truncate, validate_args, wrap_function)

BUILTIN_TOOLS: list[Tool] = [*workspace.TOOLS, *documents.TOOLS, *shell.TOOLS, *web.TOOLS,
                             *ledger_tools.TOOLS, *platform.TOOLS]


def builtin_registry() -> ToolRegistry:
    return ToolRegistry(BUILTIN_TOOLS)


__all__ = ["BUILTIN_TOOLS", "CommandResult", "FetchResult", "Tool", "ToolContext", "ToolRegistry",
           "builtin_registry", "tools_from_defs", "truncate", "validate_args", "wrap_function"]
