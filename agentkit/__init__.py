"""
agentkit - the shared, provider-neutral kit that every first-party specialist
agent on Agent's List runs on.

A specialist is a manifest (agent.yaml), prompts, domain tools and acceptance
checks; the kit supplies the model adapters, the tool loop, sandboxed tools,
the claim ledger, checks, evidence hashing and the harness CLI
(`python -m agentkit`). Flask-free, like chain/. See
docs/decisions/0002-specialist-kit.md.
"""
from __future__ import annotations

from agentkit.checks import CheckContext, CheckRegistry, default_registry, run_checks
from agentkit.errors import (AgentKitError, BudgetExceeded, ManifestError, ModelError,
                             PolicyViolation, ToolError)
from agentkit.events import Event, EventSink, JsonlSink, MemorySink, MultiSink, NullSink
from agentkit.evidence import (artifact_for, canonical_json, evidence_hash, platform_evidence,
                               sha256_file)
from agentkit.journal import Checkpoint, Journal
from agentkit.ledger import Ledger
from agentkit.llm import ModelAdapter, ModelRef, ScriptedAdapter, get_adapter
from agentkit.loop import Limits, RunOutcome, Runner
from agentkit.manifest import Manifest, load_manifest, operator_fields, parse_manifest, spec_hash
from agentkit.policy import PolicyGate
from agentkit.registry import list_specialists, load_specialist
from agentkit.security import host_allowed, redact, wrap_untrusted
from agentkit.specialist import RunContext, Specialist, build_adapter, resolve_model_refs
from agentkit.tools import (CommandResult, FetchResult, Tool, ToolContext, ToolRegistry,
                            builtin_registry, tools_from_defs)
from agentkit.types import (AcceptanceCriterion, Artifact, Brief, CheckResult, Estimate,
                            HumanReview, Message, MilestoneSpec, MissingInput, ModelResponse,
                            Submission, ToolCall, ToolResult, ToolSpec, Usage)

__all__ = [
    "AcceptanceCriterion", "AgentKitError", "Artifact", "Brief", "BudgetExceeded",
    "CheckContext", "CheckRegistry", "CheckResult", "Checkpoint", "CommandResult", "Estimate",
    "Event", "EventSink", "FetchResult", "HumanReview", "Journal", "JsonlSink", "Ledger",
    "Limits", "Manifest", "ManifestError", "MemorySink", "Message", "MilestoneSpec",
    "MissingInput", "ModelAdapter", "ModelError", "ModelRef", "ModelResponse", "MultiSink",
    "NullSink", "PolicyGate", "PolicyViolation", "RunContext", "RunOutcome", "Runner",
    "ScriptedAdapter", "Specialist", "Submission", "Tool", "ToolCall", "ToolContext",
    "ToolError", "ToolRegistry", "ToolResult", "ToolSpec", "Usage", "artifact_for",
    "build_adapter", "builtin_registry", "canonical_json", "default_registry", "evidence_hash",
    "get_adapter", "host_allowed", "list_specialists", "load_manifest", "load_specialist",
    "operator_fields", "parse_manifest", "platform_evidence", "redact", "resolve_model_refs", "run_checks",
    "sha256_file", "spec_hash", "tools_from_defs", "wrap_untrusted",
]
