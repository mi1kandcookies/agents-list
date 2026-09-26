"""
agentkit/ledger.py - the claim ledger: every factual claim points at a
verbatim quote in a saved source.

    .agentkit/ledger.json        {"sources": [...], "claims": [...]}
    .agentkit/sources/<id>.txt   the text snapshot each quote is checked against

A source is registered with the text the kit itself read: a fetched page
(kind "web"), a customer file under inputs/ or repo/ (kind "customer"), or
data a domain tool fetched (kind "tool"). A claim must quote that snapshot
verbatim, after whitespace and Unicode normalization, at the moment it is
recorded. Deliverables cite claims as [C1], [C2], ...

Files the agent writes (write_file, edit_file, a domain tool's
resolve_path(..., write=True)) are listed in "authored" and can never become
a source, so the model cannot ground a claim in its own notes.

verify() re-checks every claim, and each snapshot against the sha256 kept
with its source, so editing a claim's quote or a snapshot on its own is
caught by the ledger_verified acceptance check. This is tamper-evidence
against mistakes and single-file edits, not integrity against a process
that can rewrite both files: code run through an allowlisted interpreter can,
and only an OS sandbox (0002, "Containment") prevents that.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentkit.errors import ToolError
from agentkit.journal import write_json_atomic

_WS = re.compile(r"\s+")
MIN_QUOTE_CHARS = 8


def normalize_text(text: str) -> str:
    """NFKC, curly quotes straightened, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text or "")
    text = (text.replace("‘", "'").replace("’", "'")
                .replace("“", '"').replace("”", '"'))
    return _WS.sub(" ", text).strip()


SOURCE_KINDS = ("customer", "web", "tool")


@dataclass
class Source:
    id: str
    uri: str
    title: str
    retrieved_at: str
    kind: str = "tool"       # customer | web | tool
    sha256: str = ""         # of the UTF-8 snapshot; "" in ledgers written before hashing


@dataclass
class Claim:
    id: str
    text: str
    source: str
    quote: str
    location: str = ""


class Ledger:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.root = self.workspace / ".agentkit"
        self.path = self.root / "ledger.json"
        self.sources: list[Source] = []
        self.claims: list[Claim] = []
        self.authored: list[str] = []    # workspace paths the agent wrote
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        known_s = {f.name for f in fields(Source)}
        known_c = {f.name for f in fields(Claim)}
        self.sources = [Source(**{k: v for k, v in s.items() if k in known_s})
                        for s in data.get("sources", [])]
        self.claims = [Claim(**{k: v for k, v in c.items() if k in known_c})
                       for c in data.get("claims", [])]
        self.authored = [str(p) for p in data.get("authored", [])]

    def save(self) -> None:
        write_json_atomic(self.path, self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {"sources": [asdict(s) for s in self.sources],
                "claims": [asdict(c) for c in self.claims],
                "authored": list(self.authored)}

    # --- files the agent wrote ------------------------------------------------------

    def note_authored(self, rel: str) -> None:
        """Record that the agent wrote workspace path `rel` (forward slashes)."""
        if rel not in self.authored:
            self.authored.append(rel)
            self.save()

    def is_authored(self, rel: str) -> bool:
        """Compared case-insensitively: on a case-insensitive filesystem
        "repo/NOTES.md" and "repo/notes.md" are the same file (erring toward
        "authored" costs nothing on a case-sensitive one)."""
        key = rel.replace("\\", "/").casefold()
        return any(p.replace("\\", "/").casefold() == key for p in self.authored)

    # --- sources ---------------------------------------------------------------

    def snapshot_path(self, source_id: str) -> Path:
        return self.root / "sources" / f"{source_id}.txt"

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def snapshot_bytes(self, source_id: str) -> bytes | None:
        path = self.snapshot_path(source_id)
        return path.read_bytes() if path.exists() else None

    def snapshot(self, source_id: str) -> str:
        data = self.snapshot_bytes(source_id)
        return data.decode("utf-8", errors="replace") if data is not None else ""

    def add_source(self, uri: str, title: str, text: str, *, retrieved_at: str | None = None,
                   kind: str = "tool") -> Source:
        """Register a source and save its text snapshot (bytes as given, no
        newline translation). Re-registering the same uri with identical text
        returns the existing source."""
        if not uri.strip():
            raise ToolError("source uri must not be empty")
        if kind not in SOURCE_KINDS:
            raise ValueError(f"source kind must be one of {', '.join(SOURCE_KINDS)}")
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        for s in self.sources:
            if s.uri == uri and self.snapshot_bytes(s.id) == data:
                return s
        src = Source(id=f"S{len(self.sources) + 1}", uri=uri, title=title or uri,
                     retrieved_at=retrieved_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     kind=kind, sha256=digest)
        path = self.snapshot_path(src.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.sources.append(src)
        self.save()
        return src

    def snapshot_problem(self, source_id: str) -> str | None:
        """Why a source's snapshot cannot be trusted, or None."""
        src = self.source(source_id)
        if src is None:
            return f"unknown source {source_id!r}"
        data = self.snapshot_bytes(source_id)
        if data is None:
            return f"snapshot of {source_id} is missing"
        if src.sha256 and hashlib.sha256(data).hexdigest() != src.sha256:
            return f"snapshot of {source_id} changed after it was recorded"
        if src.uri.startswith("workspace:") and self.is_authored(src.uri[len("workspace:"):]):
            return f"source {source_id} is a file written during the engagement"
        return None

    # --- claims -------------------------------------------------------------------

    def claim(self, claim_id: str) -> Claim | None:
        return next((c for c in self.claims if c.id == claim_id), None)

    def quote_problem(self, source_id: str, quote: str) -> str | None:
        """Why `quote` does not verify against the source, or None if it does."""
        if self.source(source_id) is None:
            return f"unknown source {source_id!r}"
        needle = normalize_text(quote)
        if len(needle) < MIN_QUOTE_CHARS:
            return f"quote must be at least {MIN_QUOTE_CHARS} characters"
        if needle not in normalize_text(self.snapshot(source_id)):
            return f"quote not found verbatim in source {source_id}"
        return None

    def add_claim(self, text: str, source: str, quote: str, location: str = "") -> Claim:
        if not text.strip():
            raise ToolError("claim text must not be empty")
        problem = self.quote_problem(source, quote)
        if problem:
            raise ToolError(problem)
        claim = Claim(id=f"C{len(self.claims) + 1}", text=text, source=source,
                      quote=quote, location=location)
        self.claims.append(claim)
        self.save()
        return claim

    def verify(self) -> list[str]:
        """Re-check every claim (source snapshot intact and not agent-written,
        quote present); return one problem string per bad claim."""
        problems = []
        ids = set()
        for c in self.claims:
            if c.id in ids:
                problems.append(f"{c.id}: duplicate claim id")
            ids.add(c.id)
            problem = self.snapshot_problem(c.source) or self.quote_problem(c.source, c.quote)
            if problem:
                problems.append(f"{c.id}: {problem}")
        return problems


__all__ = ["Claim", "Ledger", "MIN_QUOTE_CHARS", "SOURCE_KINDS", "Source", "normalize_text"]
