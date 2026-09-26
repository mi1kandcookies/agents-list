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

from agentkit.errors import (AgentKitError, BudgetExceeded, ManifestError, ModelError,
                             PolicyViolation, ToolError)
from agentkit.llm import ModelAdapter, ModelRef, ScriptedAdapter
from agentkit.types import (AcceptanceCriterion, Artifact, Brief, CheckResult, Estimate,
                            HumanReview, Message, MilestoneSpec, MissingInput, ModelResponse,
                            Submission, ToolCall, ToolResult, ToolSpec, Usage)

__all__ = [
    "AcceptanceCriterion", "AgentKitError", "Artifact", "Brief", "BudgetExceeded", "CheckResult",
    "Estimate", "HumanReview", "ManifestError", "Message", "MilestoneSpec", "MissingInput",
    "ModelAdapter", "ModelError", "ModelRef", "ModelResponse", "PolicyViolation", "ScriptedAdapter",
    "Submission", "ToolCall", "ToolError", "ToolResult", "ToolSpec", "Usage",
]
