"""
agentkit/tools/ledger_tools.py - record_source and record_claim.

record_source registers customer material only: a file under inputs/ or
repo/ that the agent did not write (the ledger lists every file written
through write_file / edit_file / a domain tool's resolve_path). Web pages are
registered by http_fetch; domain tools register fetched data through their
ledger helper. The model cannot hand in free text or its own notes as a
"source": the snapshot is always what the kit itself read, so a quote can
only verify against text the client or a fetched page really contains.
"""
from __future__ import annotations

from typing import Any

from agentkit.errors import PolicyViolation, ToolError
from agentkit.tools.base import Tool, ToolContext
from agentkit.tools.documents import document_text

# Top-level workspace folders that hold the client's material.
SOURCE_DIRS = ("inputs", "repo")


def record_source(args: dict, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.path(args["path"])
    if not path.is_file():
        raise ToolError(f"no such file: {args['path']}")
    rel = ctx.rel(path)
    if rel.split("/", 1)[0].lower() not in SOURCE_DIRS:
        raise PolicyViolation(f"{rel} is not client material; only files under "
                              f"{' or '.join(d + '/' for d in SOURCE_DIRS)} can be sources")
    if ctx.ledger.is_authored(rel):
        raise PolicyViolation(f"{rel} was written during this engagement, so it cannot be "
                              "a source; cite the material it was based on")
    src = ctx.ledger.add_source(f"workspace:{rel}", args.get("title") or rel, document_text(path),
                                kind="customer")
    return {"source_id": src.id, "uri": src.uri}


def record_claim(args: dict, ctx: ToolContext) -> dict[str, Any]:
    claim = ctx.ledger.add_claim(args["text"], args["source"], args["quote"],
                                 args.get("location") or "")
    return {"claim_id": claim.id, "cite_as": f"[{claim.id}]"}


TOOLS = [
    Tool("record_source", "Register a client file (under inputs/ or repo/) as a citable source; "
         "returns source_id. Files you wrote cannot be sources.",
         {"type": "object", "required": ["path"], "properties": {
             "path": {"type": "string"}, "title": {"type": "string"}}},
         record_source, risk="read"),
    Tool("record_claim", "Record a factual claim backed by a verbatim quote from a source. The "
         "quote is checked against the saved source text; cite the returned id as [C#].",
         {"type": "object", "required": ["text", "source", "quote"], "properties": {
             "text": {"type": "string"}, "source": {"type": "string", "description": "source_id, e.g. S1"},
             "quote": {"type": "string"}, "location": {"type": "string"}}},
         record_claim, risk="read"),
]
