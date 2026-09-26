"""
agentkit/evidence.py - hashes that tie a submission to what was delivered.

    sha256_file(path)                 hex digest of a file
    canonical_json(obj)               the platform's canonical JSON, UTF-8 bytes
    submission_record(submission)     the Submission as integers and strings only
    submission_digest(submission)     "0x" + sha256 of the canonical record
    platform_evidence(submission, milestone_idx=None)
                                      compact canonical JSON (at most 4000
                                      characters) to post as a milestone's evidence
    evidence_hash(submission, milestone_idx=None)
                                      "0x" + sha256 of that evidence text
    artifact_for(workspace, rel)      Artifact(path, sha256, bytes, media_type)

Canonical JSON follows the platform's rules (app/approvals/actions.py
canonical()), so any language can reproduce the bytes:

    - UTF-8; object keys sorted; no whitespace (separators "," and ":")
    - non-ASCII characters written as-is (json.dumps ensure_ascii=False)
    - integers only: a float anywhere is a TypeError. The record therefore
      carries cost as integer micro-USD (cost_micro_usd) and check scores as
      integer basis points (score_bps); the kit stores cost_usd rounded to 6
      decimals and scores to 4, so value * 10**6 or 10**4 is an exact
      integer however it is rounded
    - only string keys, and str, int, bool, None, lists and objects as values

Posting evidence: POST /api/engagements/<id>/milestones/<idx>/submit takes
{"evidence": text}, refuses an empty text or one over 4000 characters, strips
surrounding whitespace and returns "0x" + sha256(text.encode("utf-8")) in
lowercase hex (app/engagements/service.py submit_milestone). platform_evidence()
is such a text (no surrounding whitespace, at most EVIDENCE_MAX_CHARS
characters) and evidence_hash() computes the same value, so the evidence_hash
the platform returns equals Submission.evidence_hash character for character.
The "0x" prefix is part of the value on both sides; drop it and hex-decode to
get the 32 raw bytes. The hash is intended for the planned escrow evidence
field (#1).

The evidence text is

    {"v": 1, "kind": "agentkit.submission", "engagement_id", "milestone_idx",
     "milestone_id", "status", "human_review": bool,
     "artifacts": [{"path", "sha256", "bytes"}], "checks": [{"check", "kind", "passed"}],
     "submission": submission_digest}

When the artifact list would make it longer than the limit, "artifacts" is
replaced by "artifacts_count" and "artifacts_sha256" ("0x" + sha256 of the
canonical list); if it is still too long, "checks" is digested the same way.
The full Submission (summary, check details, usage, questions, the models
that produced the run) is bound by "submission", so anyone holding the
submission JSON and the artifact files can recompute every value. Media types come from the kit's own extension
table, never from the host, so every machine agrees.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from agentkit.types import Artifact, Submission

EVIDENCE_VERSION = 1
EVIDENCE_KIND = "agentkit.submission"
# The platform's limit on a milestone's evidence text (characters, after strip).
EVIDENCE_MAX_CHARS = 4000
SCORE_SCALE = 10_000        # basis points
COST_SCALE = 1_000_000      # micro-USD

_MEDIA_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".log": "text/plain",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".json": "application/json",
    ".jsonl": "application/jsonl", ".sarif": "application/sarif+json", ".patch": "text/x-diff",
    ".diff": "text/x-diff", ".yaml": "application/yaml", ".yml": "application/yaml",
    ".toml": "application/toml", ".xml": "application/xml", ".html": "text/html", ".htm": "text/html",
    ".sql": "application/sql", ".py": "text/x-python", ".js": "text/javascript",
    ".ts": "text/x-typescript", ".sh": "text/x-shellscript", ".ipynb": "application/x-ipynb+json",
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".zip": "application/zip", ".gz": "application/gzip",
    ".sqlite": "application/vnd.sqlite3", ".db": "application/vnd.sqlite3",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_json(obj: Any, path: str = "$") -> None:
    """Reject values whose JSON encoding is ambiguous across languages."""
    if isinstance(obj, float):
        raise TypeError(f"float at {path}: use integer units")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"non-string key {k!r} at {path}")
            _check_json(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_json(v, f"{path}[{i}]")
    elif obj is not None and not isinstance(obj, (str, int, bool)):
        raise TypeError(f"unsupported type {type(obj).__name__} at {path}")


def canonical_json(obj: Any) -> bytes:
    """The platform's canonical JSON (see the module docstring)."""
    _check_json(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(obj: Any) -> str:
    return "0x" + hashlib.sha256(canonical_json(obj)).hexdigest()


def media_type(path: str | Path) -> str:
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def artifact_for(workspace: Path, rel: str) -> Artifact:
    path = Path(workspace) / rel
    return Artifact(path=Path(rel).as_posix(), sha256=sha256_file(path), bytes=path.stat().st_size,
                    media_type=media_type(rel))


def to_units(value: Any, scale: int, what: str) -> int | None:
    """A float amount as an integer number of 1/scale units (None stays None)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError(f"{what} must be a finite number, got {value!r}")
    return round(value * scale)


def _submission(submission: Submission | dict[str, Any]) -> Submission:
    return submission if isinstance(submission, Submission) else Submission.from_dict(dict(submission))


def submission_record(submission: Submission | dict[str, Any]) -> dict[str, Any]:
    """The Submission (without evidence_hash) as integers and strings only."""
    s = _submission(submission)
    u, hr = s.usage, s.human_review
    return {
        "v": EVIDENCE_VERSION,
        "engagement_id": s.engagement_id, "milestone_id": s.milestone_id,
        "milestone_idx": s.milestone_idx, "status": s.status, "summary": s.summary,
        "artifacts": [{"path": a.path, "sha256": a.sha256, "bytes": a.bytes, "media_type": a.media_type}
                      for a in s.artifacts],
        "check_results": [{"check": r.check, "kind": r.kind, "passed": r.passed, "details": r.details,
                           "score_bps": to_units(r.score, SCORE_SCALE, f"{r.check} score")}
                          for r in s.check_results],
        "human_review": {"required": hr.required, "reviewer_role": hr.reviewer_role,
                         "checklist": list(hr.checklist), "disclaimer": hr.disclaimer},
        "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                  "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens,
                  "cost_micro_usd": to_units(u.cost_usd, COST_SCALE, "cost_usd")},
        "questions": list(s.questions),
        "models": list(s.models),
    }


def submission_digest(submission: Submission | dict[str, Any]) -> str:
    return _digest(submission_record(submission))


def platform_evidence(submission: Submission | dict[str, Any], milestone_idx: int | None = None) -> str:
    """The evidence text to post for the SOW milestone `milestone_idx`
    (default: the submission's own milestone_idx; the two must agree)."""
    s = _submission(submission)
    if milestone_idx is None:
        milestone_idx = s.milestone_idx
    elif s.milestone_idx is not None and s.milestone_idx != milestone_idx:
        raise ValueError(f"submission is for milestone_idx {s.milestone_idx}, not {milestone_idx}")
    if isinstance(milestone_idx, bool) or not isinstance(milestone_idx, int) or milestone_idx < 0:
        raise ValueError(f"milestone_idx must be an integer >= 0, got {milestone_idx!r}")
    record = submission_record(s)   # validates the whole submission first
    record["milestone_idx"] = milestone_idx
    artifacts = [{"path": a.path, "sha256": a.sha256, "bytes": a.bytes} for a in s.artifacts]
    checks = [{"check": r.check, "kind": r.kind, "passed": r.passed} for r in s.check_results]
    base = {"v": EVIDENCE_VERSION, "kind": EVIDENCE_KIND, "engagement_id": s.engagement_id,
            "milestone_idx": milestone_idx, "milestone_id": s.milestone_id, "status": s.status,
            "human_review": s.human_review.required, "submission": _digest(record)}
    full = {"artifacts": artifacts, "checks": checks}
    lean_artifacts = {"artifacts_count": len(artifacts), "artifacts_sha256": _digest(artifacts),
                      "checks": checks}
    leanest = {**lean_artifacts, "checks_count": len(checks), "checks_sha256": _digest(checks)}
    del leanest["checks"]
    for body in (full, lean_artifacts, leanest):
        text = canonical_json({**base, **body}).decode("utf-8")
        if len(text) <= EVIDENCE_MAX_CHARS:
            return text
    raise ValueError(f"evidence is longer than {EVIDENCE_MAX_CHARS} characters even with digests "
                     "(engagement or milestone id too long)")


def evidence_hash(submission: Submission | dict[str, Any], milestone_idx: int | None = None) -> str:
    """ "0x" + sha256 of platform_evidence(): the value the platform's submit
    endpoint returns for that text."""
    text = platform_evidence(submission, milestone_idx)
    return "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["COST_SCALE", "EVIDENCE_KIND", "EVIDENCE_MAX_CHARS", "EVIDENCE_VERSION", "SCORE_SCALE",
           "artifact_for", "canonical_json", "evidence_hash", "media_type", "platform_evidence",
           "sha256_file", "submission_digest", "submission_record", "to_units"]
