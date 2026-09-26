"""
agentkit/tools/platform.py - talking to the platform: ask_client,
post_progress and submit_milestone.

None of these reaches the client directly. Questions and progress become
events the platform relays; answers come back in Brief.answers on a later
run. submit_milestone ends the milestone: it records the summary and the
artifact list for the harness, which runs the acceptance checks.
"""
from __future__ import annotations

from typing import Any

from agentkit.errors import ToolError
from agentkit.tools.base import Tool, ToolContext


def ask_client(args: dict, ctx: ToolContext) -> str:
    question = args["question"].strip()
    if not question:
        raise ToolError("question must not be empty")
    answers = ctx.brief.answers if ctx.brief else {}
    if question in answers:
        return f"The client answered: {answers[question]}"
    ctx.state.setdefault("questions", []).append(question)
    ctx.events.emit("question", question=question, blocking=bool(args.get("blocking")))
    return ("Question recorded for the client. Answers arrive on a later run; continue with "
            "what you can do now and state your assumptions in the deliverable.")


def post_progress(args: dict, ctx: ToolContext) -> str:
    ctx.events.emit("progress", message=args["message"])
    return "ok"


def submit_milestone(args: dict, ctx: ToolContext) -> dict[str, Any]:
    artifacts = []
    for p in args.get("artifacts") or []:
        path = ctx.path(p)
        if not path.is_file():
            raise ToolError(f"artifact not found: {p}")
        artifacts.append(ctx.rel(path))
    if not artifacts:
        raise ToolError("submit at least one artifact (the deliverable files)")
    ctx.state["submitted"] = {"summary": args["summary"], "artifacts": artifacts}
    ctx.events.emit("submitted", artifacts=artifacts)
    return {"status": "submitted", "artifacts": artifacts,
            "note": "Acceptance checks run next; stop here."}


TOOLS = [
    Tool("ask_client", "Ask the client a question through the platform (not answered in this run "
         "unless already in the brief).",
         {"type": "object", "required": ["question"], "properties": {
             "question": {"type": "string"}, "blocking": {"type": "boolean"}}},
         ask_client, risk="read"),
    Tool("post_progress", "Post a short progress note to the client's engagement timeline.",
         {"type": "object", "required": ["message"], "properties": {"message": {"type": "string"}}},
         post_progress, risk="read"),
    Tool("submit_milestone", "Submit the milestone: a summary and the workspace paths of the "
         "deliverable files. Call once, when the deliverables are complete.",
         {"type": "object", "required": ["summary", "artifacts"], "properties": {
             "summary": {"type": "string"},
             "artifacts": {"type": "array", "items": {"type": "string"}}}},
         submit_milestone, risk="write"),
]
