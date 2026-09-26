"""
agentkit/types.py - provider-neutral data types shared by the kit, the
specialists and the platform.

Every type round-trips through plain JSON with to_dict() / from_dict(), so a
Brief can come from the scoping service, a checkpoint can survive a VM
restart, and a Submission can be hashed and posted to the platform as
milestone evidence (agentkit.evidence). from_dict() ignores unknown keys so
older readers accept newer records.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]
StopReason = Literal["end", "tool_use", "max_tokens", "refusal", "pause"]
CheckKind = Literal["automated", "rubric", "human"]
ToolRisk = Literal["read", "write", "exec", "network", "external"]
SubmissionStatus = Literal["ready_for_review", "needs_revision", "incomplete", "budget_exceeded"]


def _plain(value: Any) -> Any:
    """asdict() output with tuples turned into lists, so json.dumps is stable."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class _Record:
    """to_dict/from_dict for flat dataclasses; nested ones override from_dict."""

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in data.items() if k in known})


# --- model conversation ---------------------------------------------------

@dataclass
class ToolCall(_Record):
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Set when the model sent arguments that were not valid JSON; the loop
    # answers such a call with an error result instead of running the tool.
    invalid: str | None = None


@dataclass
class ToolResult(_Record):
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Message(_Record):
    """One conversation turn.

    user:      text
    assistant: text and/or tool_calls; `raw` holds the provider-native content
               ({"provider", "model", "content"}) so it can be replayed
               verbatim to the same provider and model (Claude thinking blocks
               must come back unchanged)
    tool:      tool_results for every call of the preceding assistant turn
    """
    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    @classmethod
    def user(cls, text: str) -> "Message":
        return cls(role="user", text=text)

    @classmethod
    def tool(cls, results: list[ToolResult]) -> "Message":
        return cls(role="tool", tool_results=list(results))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            role=data["role"],
            text=data.get("text", ""),
            tool_calls=[ToolCall.from_dict(c) for c in data.get("tool_calls", [])],
            tool_results=[ToolResult.from_dict(r) for r in data.get("tool_results", [])],
            raw=data.get("raw"),
        )


@dataclass
class ToolSpec(_Record):
    """What the model sees of a tool: name, description and JSON Schema input."""
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class Usage(_Record):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # None when no call had known pricing; otherwise the sum of the calls
    # whose price was known (a lower bound if some were unknown). A
    # Submission stores it rounded to 6 decimals and hashes it as integer
    # micro-USD.
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: "Usage") -> "Usage":
        if self.cost_usd is None and other.cost_usd is None:
            cost = None
        else:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=cost,
        )


@dataclass
class ModelResponse(_Record):
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: Usage
    model: str                            # the model that produced this response
    raw: dict[str, Any] | None = None
    # Provider-side model switches during this call, [{"from": ref, "to": ref}]
    # (e.g. a Claude server-side fallback); the loop journals each one.
    fallbacks: list[dict[str, str]] = field(default_factory=list)

    def to_message(self) -> Message:
        return Message(role="assistant", text=self.text,
                       tool_calls=list(self.tool_calls), raw=self.raw)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelResponse":
        return cls(
            text=data.get("text", ""),
            tool_calls=[ToolCall.from_dict(c) for c in data.get("tool_calls", [])],
            stop_reason=data.get("stop_reason", "end"),
            usage=Usage.from_dict(data.get("usage", {})),
            model=data.get("model", ""),
            raw=data.get("raw"),
            fallbacks=[dict(f) for f in data.get("fallbacks", [])],
        )


# --- engagement: brief, milestones, acceptance ------------------------------

@dataclass
class AcceptanceCriterion(_Record):
    """One acceptance check for a milestone.

    automated: deterministic, re-runnable by the platform or the buyer
    rubric:    scored by a grader model against a rubric file
    human:     needs a person (e.g. a licensed reviewer); always pending here
    """
    check: str
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    kind: CheckKind = "automated"


@dataclass
class MilestoneSpec(_Record):
    id: str
    title: str
    description: str = ""
    deliverables: list[str] = field(default_factory=list)
    acceptance: list[AcceptanceCriterion] = field(default_factory=list)
    hours: tuple[float, float] = (0.0, 0.0)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MilestoneSpec":
        lo, hi = data.get("hours") or (0.0, 0.0)
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            deliverables=list(data.get("deliverables", [])),
            acceptance=[AcceptanceCriterion.from_dict(a) for a in data.get("acceptance", [])],
            hours=(float(lo), float(hi)),
        )


@dataclass
class Brief(_Record):
    """What an engagement hands the specialist: the signed scope in data form."""
    engagement_id: str
    specialist: str
    objective: str
    intake: dict[str, Any] = field(default_factory=dict)
    milestones: list[MilestoneSpec] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)  # replies to ask_client questions
    sow_text: str = ""

    def milestone(self, milestone_id: str) -> MilestoneSpec | None:
        return next((m for m in self.milestones if m.id == milestone_id), None)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Brief":
        return cls(
            engagement_id=data["engagement_id"],
            specialist=data["specialist"],
            objective=data["objective"],
            intake=dict(data.get("intake", {})),
            milestones=[MilestoneSpec.from_dict(m) for m in data.get("milestones", [])],
            answers=dict(data.get("answers", {})),
            sow_text=data.get("sow_text", ""),
        )


@dataclass
class MissingInput(_Record):
    field: str
    question: str
    blocking: bool = True


@dataclass
class Estimate(_Record):
    hours_low: float
    hours_high: float
    cost_usd_low: float     # cost to serve (model tokens + compute)
    cost_usd_high: float
    price_usd_low: float    # what the listing typically charges
    price_usd_high: float
    assumptions: list[str] = field(default_factory=list)


# --- results ------------------------------------------------------------------

@dataclass
class Artifact(_Record):
    path: str          # workspace-relative, forward slashes
    sha256: str
    bytes: int
    media_type: str


@dataclass
class CheckResult(_Record):
    check: str
    passed: bool | None      # None = pending (rubric without a grader, human sign-off)
    kind: CheckKind = "automated"
    details: str = ""
    # A fraction, normally 0-1. run_check rounds it to 4 decimals (None if
    # not a finite number); the evidence hashes it as integer basis points.
    score: float | None = None


@dataclass
class HumanReview(_Record):
    required: bool
    reviewer_role: str = ""
    checklist: list[str] = field(default_factory=list)
    disclaimer: str = ""


@dataclass
class Submission(_Record):
    engagement_id: str
    milestone_id: str
    status: SubmissionStatus
    summary: str
    artifacts: list[Artifact] = field(default_factory=list)
    check_results: list[CheckResult] = field(default_factory=list)
    human_review: HumanReview = field(default_factory=lambda: HumanReview(required=False))
    usage: Usage = field(default_factory=Usage)
    questions: list[str] = field(default_factory=list)
    # "0x" + 64 lowercase hex chars: sha256 of the milestone evidence text
    # (agentkit.evidence.platform_evidence), i.e. the value the platform's
    # submit endpoint returns for it; intended for the planned escrow
    # evidence field (#1).
    evidence_hash: str = ""
    # Index of the SOW milestone this delivers (the platform's milestone idx).
    milestone_idx: int | None = None
    # The models that produced the run's turns, as "provider:model" refs in
    # order of first use; the evidence binds them.
    models: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Submission":
        return cls(
            engagement_id=data["engagement_id"],
            milestone_id=data["milestone_id"],
            status=data["status"],
            summary=data.get("summary", ""),
            artifacts=[Artifact.from_dict(a) for a in data.get("artifacts", [])],
            check_results=[CheckResult.from_dict(c) for c in data.get("check_results", [])],
            # A record without human_review fails closed: a person must review it.
            human_review=HumanReview.from_dict(data.get("human_review") or {"required": True}),
            usage=Usage.from_dict(data.get("usage", {})),
            questions=list(data.get("questions", [])),
            evidence_hash=data.get("evidence_hash", ""),
            milestone_idx=data.get("milestone_idx"),
            models=list(data.get("models", [])),
        )
