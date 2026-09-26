"""
tests/agentkit/fixtures/example_specialist/checks.py - one domain check:
every claim in the ledger is cited in the brief.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def cites_every_claim(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    ledger_path = Path(workspace) / ".agentkit" / "ledger.json"
    claims = json.loads(ledger_path.read_text(encoding="utf-8"))["claims"] if ledger_path.exists() else []
    text = (Path(workspace) / params["path"]).read_text(encoding="utf-8")
    cited = set(re.findall(r"\[(C\d+)\]", text))
    uncited = [c["id"] for c in claims if c["id"] not in cited]
    return {"passed": not uncited and bool(claims),
            "details": f"uncited claims: {', '.join(uncited)}" if uncited else f"{len(claims)} claims cited",
            "score": None}


CHECK_DEFS = {"cites_every_claim": cites_every_claim}
