"""
tests/agentkit/fixtures/example_specialist/agent.py - the example specialist.

Everything comes from agent.yaml, tools.py and checks.py beside this file;
the subclass only exists so the registry can find it.
"""
from __future__ import annotations

from agentkit.specialist import Specialist


class ExampleSpecialist(Specialist):
    pass
