"""Small, deterministic deliverable builders for protected paid tasks.

The QA specialist turns the buyer-owned API specification into a reviewable
test-plan shape. It does not execute requests or code. The source hash is
returned with the result so the delivered plan can be tied to the exact text
that was included in the paid, screened task.
"""
from __future__ import annotations

import hashlib
import re


_OPERATION = re.compile(
    r"(?im)^\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+([^\s,;]+)"
)
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"})


def _operations(spec: str) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for match in _OPERATION.finditer(spec):
        method, path = match.group(1).upper(), match.group(2).rstrip(".`)")
        if method not in _METHODS or not path.startswith("/"):
            continue
        item = (method, path)
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_api_test_plan(spec: str, *, specialist: str, agent_id: str) -> dict:
    """Build a structured plan from the exact supplied specification.

    This intentionally stays deterministic for the demo and for offline
    verification. A future specialist adapter can replace the planner while
    retaining this response contract and source binding.
    """
    source = str(spec or "")
    operations = _operations(source)
    if not operations:
        operations = [("SPEC", "(supplied task text)")]

    cases: list[dict] = []
    for index, (method, path) in enumerate(operations, start=1):
        prefix = f"TC-{index:03d}"
        if method == "SPEC":
            cases.append({
                "id": prefix,
                "method": None,
                "path": path,
                "scenario": "Clarify the supplied task before implementing tests",
                "priority": "high",
                "assertions": [
                    "Identify the contract, inputs, outputs and acceptance criteria",
                    "Record missing examples as explicit test-plan assumptions",
                ],
            })
            continue
        cases.extend([
            {
                "id": prefix,
                "method": method,
                "path": path,
                "scenario": "documented success response",
                "priority": "high",
                "assertions": [
                    f"{method} {path} returns the documented success status",
                    "Response content type and required fields match the specification",
                ],
            },
            {
                "id": f"TC-{index:03d}-V",
                "method": method,
                "path": path,
                "scenario": "invalid or incomplete input",
                "priority": "high",
                "assertions": [
                    "Missing and malformed required inputs are rejected",
                    "The error response is stable and does not perform a side effect",
                ],
            },
            {
                "id": f"TC-{index:03d}-A",
                "method": method,
                "path": path,
                "scenario": "authorization boundary",
                "priority": "high" if method in {"POST", "PUT", "PATCH", "DELETE"} else "medium",
                "assertions": [
                    "Unauthenticated and unauthorized callers receive the documented denial",
                    "A denied request does not change server state",
                ],
            },
        ])

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return {
        "type": "api_test_plan",
        "version": 1,
        "specialist": specialist,
        "agent_id": agent_id,
        "source": {
            "sha256": "0x" + digest,
            "bytes": len(source.encode("utf-8")),
            "endpoint_count": len([item for item in operations if item[0] != "SPEC"]),
        },
        "test_case_count": len(cases),
        "test_cases": cases,
        "safety_note": "Plan only: no API request or arbitrary code is executed.",
    }
