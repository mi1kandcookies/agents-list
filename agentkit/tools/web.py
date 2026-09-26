"""
agentkit/tools/web.py - http_fetch and web_search.

http_fetch goes through ctx.fetch (egress allowlist, redirects re-checked,
size cap), turns HTML into text and registers the page in the claim ledger,
so the model can quote it with record_claim. web_search calls a pluggable
provider in ctx.services["search"]; none is configured by default.
"""
from __future__ import annotations

from typing import Any

from agentkit.errors import ToolError
from agentkit.tools.base import Tool, ToolContext, truncate
from agentkit.tools.documents import html_to_text


def http_fetch(args: dict, ctx: ToolContext) -> dict[str, Any]:
    res = ctx.fetch(args["url"])
    ctype = {k.lower(): v for k, v in res.headers.items()}.get("content-type", "")
    title, text = "", res.text
    if "html" in ctype or res.text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
        title, text = html_to_text(res.text)
    out: dict[str, Any] = {"url": res.url, "status": res.status, "title": title,
                           "truncated": res.truncated}
    if 200 <= res.status < 300 and text.strip() and ctx.ledger is not None:
        out["source_id"] = ctx.ledger.add_source(res.url, title or res.url, text, kind="web").id
    max_chars = int(args.get("max_chars") or ctx.max_output_chars)
    out["text"] = truncate(text, min(max_chars, ctx.max_output_chars))
    return out


def web_search(args: dict, ctx: ToolContext) -> dict[str, Any]:
    provider = ctx.services.get("search")
    if provider is None:
        raise ToolError("web search is not configured for this run; use http_fetch on known URLs")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    results = provider(args["query"], limit)
    return {"results": [{"title": r.get("title", ""), "url": r.get("url", ""),
                         "snippet": r.get("snippet", "")} for r in results[:limit]]}


TOOLS = [
    Tool("http_fetch", "Fetch an allowlisted http(s) URL. HTML is converted to text and the page "
         "is saved as a ledger source (source_id) for record_claim.",
         {"type": "object", "required": ["url"], "properties": {
             "url": {"type": "string"}, "max_chars": {"type": "integer"}}},
         http_fetch, risk="network", untrusted_output=True),
    Tool("web_search", "Search the web (if a search provider is configured). Returns title, "
         "url and snippet per result.",
         {"type": "object", "required": ["query"], "properties": {
             "query": {"type": "string"}, "limit": {"type": "integer"}}},
         web_search, risk="network", untrusted_output=True),
]
