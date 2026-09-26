"""A safe QA specialist deliverable: structured test cases, no code execution."""
from __future__ import annotations

import json
import re


def _fallback_plan(task: str, *, specialist_name: str) -> dict:
    endpoints = re.findall(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s`]+)", task, re.I)
    cases = []
    for method, path in endpoints:
        method, path = method.upper(), path.rstrip(".,")
        case_number = len(cases) + 1
        cases.extend([
            {"id": f"{method.lower()}-{case_number:02d}", "method": method,
             "path": path, "title": "accepts a valid request", "expected": "2xx response"},
            {"id": f"{method.lower()}-{case_number+1:02d}", "method": method,
             "path": path, "title": "rejects an invalid or incomplete request",
             "expected": "4xx response with a stable error body"},
        ])
    if not cases:
        cases = [{"id": "spec-01", "method": "N/A", "path": "N/A",
                  "title": "turn every requirement in the supplied specification into an executable test",
                  "expected": "each requirement has a positive and negative case"}]
    return {
        "title": "API specification test plan",
        "specialist": specialist_name,
        "source": "server-owned task specification",
        "testCases": cases,
        "notes": ["No arbitrary code was executed.", "Run the cases in an isolated test environment."],
    }


def generate_test_plan(task: str, *, specialist_name: str, category: str = "Testing") -> dict:
    from app.llm import generate

    try:
        generated = generate(
            task, agent_name=specialist_name, agent_category=category,
            agent_bio="Produces structured API test plans from a supplied specification. "
                       "Never executes arbitrary code.", max_tokens=900, temperature=0.2,
        )
        content = generated.get("response", "")
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                parsed.setdefault("specialist", specialist_name)
                parsed.setdefault("generatedBy", generated.get("model"))
                return parsed
        except (TypeError, ValueError):
            pass
        return {"title": "API specification test plan", "specialist": specialist_name,
                "format": "markdown", "content": content, "generatedBy": generated.get("model")}
    except RuntimeError:
        return _fallback_plan(task, specialist_name=specialist_name)
