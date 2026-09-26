"""
agentkit/errors.py - exception hierarchy for the specialist kit.

Every error the kit raises on purpose derives from AgentKitError, so the
harness can tell "the agent hit a limit or a policy" apart from a bug.
"""
from __future__ import annotations


class AgentKitError(Exception):
    """Base class for errors raised deliberately by the kit."""


class ManifestError(AgentKitError):
    """An agent.yaml is missing a field, has a wrong type, or an unknown key.

    `path` is a dotted location such as "milestones[1].acceptance[0].check".
    """

    def __init__(self, message: str, path: str = ""):
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class ModelError(AgentKitError):
    """A model call failed after the provider SDK's own retries.

    `retryable` is True for rate limits, timeouts, 5xx and connection errors;
    a FallbackAdapter moves to the next model on any ModelError.
    """

    def __init__(self, message: str, *, provider: str = "", retryable: bool = False):
        self.provider = provider
        self.retryable = retryable
        super().__init__(message)


class BudgetExceeded(AgentKitError):
    """A run limit (steps, tokens, USD, wall time) was reached."""

    def __init__(self, limit: str, message: str = ""):
        self.limit = limit
        super().__init__(message or f"budget exceeded: {limit}")


class PolicyViolation(AgentKitError):
    """A tool call was refused by the PolicyGate (tool, path, command or host)."""


class ToolError(AgentKitError):
    """A tool failed in an expected way; the message is shown to the model."""
