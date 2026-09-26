"""
agentkit/events.py - the structured event stream a run emits.

The harness prints events as JSON lines on stdout (JsonlSink); tests collect
them in memory (MemorySink); the journal persists them (journal.Journal is a
sink too). Event types used by the kit:

    run_started, model_response, tool_call, tool_result, policy_denied,
    nudge, stuck, budget_exceeded, model_error, cost_unknown, egress,
    command, progress, question, submitted, run_finished, check_result,
    submission
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, TextIO

from agentkit.security import redact_value


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(type=data["type"], data=dict(data.get("data", {})), ts=data.get("ts", 0.0))


class EventSink:
    """Base sink: emit() builds the Event, handle() delivers it."""

    def emit(self, type: str, **data: Any) -> Event:
        event = Event(type=type, data=data)
        self.handle(event)
        return event

    def handle(self, event: Event) -> None:  # pragma: no cover - overridden
        pass


class NullSink(EventSink):
    pass


class MemorySink(EventSink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, type: str) -> list[Event]:
        return [e for e in self.events if e.type == type]

    @property
    def types(self) -> list[str]:
        return [e.type for e in self.events]


class JsonlSink(EventSink):
    """One JSON object per line, flushed immediately; known secrets redacted."""

    def __init__(self, stream: TextIO, *, secrets: Iterable[str] = ()):
        self.stream = stream
        self.secrets = list(secrets)

    def handle(self, event: Event) -> None:
        line = json.dumps(redact_value(event.to_dict(), self.secrets), default=str, ensure_ascii=False)
        self.stream.write(line + "\n")
        self.stream.flush()


class MultiSink(EventSink):
    def __init__(self, *sinks: EventSink):
        self.sinks = [s for s in sinks if s is not None]

    def handle(self, event: Event) -> None:
        for sink in self.sinks:
            sink.handle(event)


__all__ = ["Event", "EventSink", "JsonlSink", "MemorySink", "MultiSink", "NullSink"]
