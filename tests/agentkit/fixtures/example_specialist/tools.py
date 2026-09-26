"""
tests/agentkit/fixtures/example_specialist/tools.py - one domain tool, written
as a plain function the way specialist packages write them.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def word_stats(workspace: Path, *, path: str) -> dict[str, Any]:
    root = Path(workspace).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"not a workspace file: {path}")
    text = target.read_text(encoding="utf-8")
    return {"path": path, "words": len(re.findall(r"\S+", text)),
            "citations": sorted(set(re.findall(r"\[(C\d+)\]", text)))}


TOOL_DEFS = [
    {"name": "word_stats", "risk": "read", "function": word_stats,
     "description": "Count words and list [C#] citations in a workspace file.",
     "input_schema": {"type": "object", "required": ["path"], "additionalProperties": False,
                      "properties": {"path": {"type": "string"}}}},
]
