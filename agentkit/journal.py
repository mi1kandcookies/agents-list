"""
agentkit/journal.py - the append-only run journal and per-milestone checkpoints.

    .agentkit/journal.jsonl               every event, one JSON line, never rewritten
    .agentkit/checkpoints/<milestone>.json latest loop state for --resume

A Journal is also an EventSink, so wiring it into the run's sinks persists
the whole event stream, with known secrets redacted. Checkpoints are written
atomically (temp file + os.replace) after every loop step, so a VM that dies
mid-run resumes from the last completed step with the same history.
Checkpoints are not redacted: resuming must replay the history byte for byte
(an edited turn invalidates Claude's thinking blocks), so they never leave
the VM; tool results in them are already redacted when they are produced.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from agentkit.events import Event, EventSink
from agentkit.security import redact_value
from agentkit.types import Message, Usage

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def safe_name(name: str) -> str:
    """A milestone id as a file name: only [A-Za-z0-9_.-], never "." or ".."."""
    cleaned = _SAFE_ID.sub("_", name) or "_"
    return "_" if set(cleaned) == {"."} else cleaned


_safe = safe_name  # older private name


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Checkpoint:
    """Everything the Runner needs to continue a milestone."""
    milestone_id: str
    step: int = 0
    messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    nudged: bool = False
    elapsed_seconds: float = 0.0
    questions: list[str] = field(default_factory=list)
    submitted: dict[str, Any] | None = None      # submit_milestone arguments once called
    # running, or how the run ended: submitted | no_submission | budget_exceeded
    # | stuck | refused | model_error
    status: str = "running"
    # Brief.answers keys already shown to the model (in the task prompt or a
    # resume message), so a resume delivers only new answers.
    answered: list[str] = field(default_factory=list)
    # "provider:model" refs of the models that produced turns, first use first.
    models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id, "step": self.step,
            "messages": [m.to_dict() for m in self.messages],
            "usage": self.usage.to_dict(), "nudged": self.nudged,
            "elapsed_seconds": self.elapsed_seconds, "questions": list(self.questions),
            "submitted": self.submitted, "status": self.status, "answered": list(self.answered),
            "models": list(self.models),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        return cls(
            milestone_id=data["milestone_id"], step=int(data.get("step", 0)),
            messages=[Message.from_dict(m) for m in data.get("messages", [])],
            usage=Usage.from_dict(data.get("usage", {})), nudged=bool(data.get("nudged")),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            questions=list(data.get("questions", [])), submitted=data.get("submitted"),
            status=data.get("status", "running"), answered=list(data.get("answered", [])),
            models=list(data.get("models", [])),
        )


class Journal(EventSink):
    def __init__(self, root: Path, *, secrets: Iterable[str] = ()):
        """`root` is the kit's internal directory, normally <workspace>/.agentkit."""
        self.root = Path(root)
        self.path = self.root / "journal.jsonl"
        self.secrets = list(secrets)

    @classmethod
    def for_workspace(cls, workspace: Path, **kwargs: Any) -> "Journal":
        return cls(Path(workspace) / ".agentkit", **kwargs)

    # --- append-only log ----------------------------------------------------

    def handle(self, event: Event) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(redact_value(event.to_dict(), self.secrets), default=str, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def entries(self) -> Iterator[Event]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield Event.from_dict(json.loads(line))

    # --- checkpoints -----------------------------------------------------------

    def checkpoint_path(self, milestone_id: str) -> Path:
        return self.root / "checkpoints" / f"{safe_name(milestone_id)}.json"

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        write_json_atomic(self.checkpoint_path(checkpoint.milestone_id), checkpoint.to_dict())

    def load_checkpoint(self, milestone_id: str) -> Checkpoint | None:
        path = self.checkpoint_path(milestone_id)
        if not path.exists():
            return None
        return Checkpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def clear_checkpoint(self, milestone_id: str) -> None:
        self.checkpoint_path(milestone_id).unlink(missing_ok=True)


__all__ = ["Checkpoint", "Journal", "safe_name", "write_json_atomic"]
