"""
agentkit/tools/shell.py - run_command: one allowlisted executable per call.

Commands are argv lists run with shell=False (no pipes, globbing or
redirection), a scrubbed environment, the manifest's timeout and truncated
output. Each call is an independent process, so there is no shell state to
poison between steps.
"""
from __future__ import annotations

from typing import Any

from agentkit.tools.base import Tool, ToolContext


def run_command(args: dict, ctx: ToolContext) -> dict[str, Any]:
    result = ctx.run(args["argv"], cwd=args.get("cwd"), timeout=args.get("timeout_seconds"))
    return result.to_dict()


TOOLS = [
    Tool("run_command", "Run one allowlisted program as an argv list (no shell syntax). "
         "Returns exit_code, stdout, stderr and timed_out.",
         {"type": "object", "required": ["argv"], "properties": {
             "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
             "cwd": {"type": "string", "description": "Workspace-relative working directory."},
             "timeout_seconds": {"type": "number"}}},
         run_command, risk="exec", untrusted_output=True),
]
