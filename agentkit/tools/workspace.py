"""
agentkit/tools/workspace.py - file tools jailed to the engagement workspace.

read_file, write_file, edit_file (exact replace), list_files, search_files.
All paths go through ctx.path(), so escapes, writes under inputs/ and any
access to .agentkit/ are refused by the PolicyGate. Directory walks never
follow symlinks or Windows junctions, so a link inside the workspace cannot
expose files outside it. File names are customer data, so list_files output
is wrapped as untrusted like file contents.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from agentkit.errors import ToolError
from agentkit.policy import INTERNAL_DIR
from agentkit.tools.base import Tool, ToolContext

MAX_FILE_BYTES = 10_000_000
MAX_LIST = 500
MAX_MATCHES = 200


SKIP_DIRS = (".git", "__pycache__", ".venv", "node_modules")


def _is_link(entry: os.DirEntry) -> bool:
    return entry.is_symlink() or os.path.isjunction(entry.path)


def _walk(root: Path, base: Path) -> list[Path]:
    """Regular files under root, without descending into links or junctions."""
    files = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            p = Path(entry.path)
            rel = p.relative_to(base)
            if (rel.parts and rel.parts[0] == INTERNAL_DIR) or any(part in SKIP_DIRS for part in rel.parts):
                continue
            if _is_link(entry):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(p)
            elif entry.is_file(follow_symlinks=False):
                files.append(p)
    return sorted(files)


def read_file(args: dict, ctx: ToolContext) -> str:
    path = ctx.path(args["path"])
    if not path.is_file():
        raise ToolError(f"no such file: {args['path']}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ToolError("file too large; use search_files or read_document")
    text = path.read_text(encoding="utf-8", errors="replace")
    offset = max(int(args.get("offset") or 1), 1)
    limit = args.get("limit")
    lines = text.splitlines()
    if offset > 1 or limit:
        end = offset - 1 + int(limit) if limit else len(lines)
        lines = lines[offset - 1:end]
        return "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=offset))
    return text


def write_file(args: dict, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.path(args["path"], write=True)
    content = args["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    ctx.ledger.note_authored(ctx.rel(path))
    return {"path": ctx.rel(path), "bytes": len(content.encode("utf-8"))}


def edit_file(args: dict, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.path(args["path"], write=True)
    if not path.is_file():
        raise ToolError(f"no such file: {args['path']}")
    old, new = args["old_text"], args["new_text"]
    if not old:
        raise ToolError("old_text must not be empty")
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ToolError("old_text not found; read the file and copy the exact text")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_text occurs {count} times; add context or set replace_all")
    text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8", newline="\n")
    ctx.ledger.note_authored(ctx.rel(path))
    return {"path": ctx.rel(path), "replacements": count if args.get("replace_all") else 1}


def list_files(args: dict, ctx: ToolContext) -> dict[str, Any]:
    root = ctx.path(args.get("path") or ".")
    if not root.is_dir():
        raise ToolError(f"not a directory: {args.get('path')}")
    pattern = args.get("pattern") or "*"
    files = [f for f in _walk(root, ctx.workspace)
             if fnmatch.fnmatch(f.name, pattern) or fnmatch.fnmatch(ctx.rel(f), pattern)]
    listed = [{"path": ctx.rel(f), "bytes": f.stat().st_size} for f in files[:MAX_LIST]]
    return {"files": listed, "total": len(files), "truncated": len(files) > MAX_LIST}


def search_files(args: dict, ctx: ToolContext) -> dict[str, Any]:
    root = ctx.path(args.get("path") or ".")
    try:
        regex = re.compile(args["pattern"] if args.get("regex") else re.escape(args["pattern"]),
                           0 if args.get("case_sensitive") else re.IGNORECASE)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from None
    glob = args.get("glob") or "*"
    targets = [root] if root.is_file() else _walk(root, ctx.workspace)
    matches: list[str] = []
    for f in targets:
        if not fnmatch.fnmatch(f.name, glob) or f.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{ctx.rel(f)}:{n}: {line.strip()[:300]}")
                if len(matches) >= MAX_MATCHES:
                    return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}


_PATH = {"type": "string", "description": "Workspace-relative path."}

TOOLS = [
    Tool("read_file", "Read a UTF-8 text file from the workspace. Optional offset/limit "
         "(1-based lines) return numbered lines.",
         {"type": "object", "required": ["path"], "properties": {
             "path": _PATH, "offset": {"type": "integer"}, "limit": {"type": "integer"}}},
         read_file, risk="read", untrusted_output=True),
    Tool("write_file", "Create or overwrite a text file (deliverables go under deliverables/). "
         "inputs/ is read-only.",
         {"type": "object", "required": ["path", "content"], "properties": {
             "path": _PATH, "content": {"type": "string"}}},
         write_file, risk="write"),
    Tool("edit_file", "Replace exact text in a file. old_text must match exactly once unless "
         "replace_all is true.",
         {"type": "object", "required": ["path", "old_text", "new_text"], "properties": {
             "path": _PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"},
             "replace_all": {"type": "boolean"}}},
         edit_file, risk="write"),
    Tool("list_files", "List files under a workspace directory, optionally filtered by a glob.",
         {"type": "object", "properties": {"path": _PATH, "pattern": {"type": "string"}}},
         list_files, risk="read", untrusted_output=True),
    Tool("search_files", "Search text files for a literal string (or a regex with regex=true); "
         "returns path:line: text.",
         {"type": "object", "required": ["pattern"], "properties": {
             "pattern": {"type": "string"}, "path": _PATH, "glob": {"type": "string"},
             "regex": {"type": "boolean"}, "case_sensitive": {"type": "boolean"}}},
         search_files, risk="read", untrusted_output=True),
]
