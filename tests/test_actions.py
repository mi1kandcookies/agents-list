"""Canonical actions: golden vectors, float rejection, normalization, summaries."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from app.approvals.actions import (
    action_hash, action_nonce, build_action, canonical, describe, format_usdc, sow_hash,
)
from app.approvals.executors import EXECUTORS, ExecutionResult, executor, get_executor
from app.common.agent_ids import AgentIdError

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "action_vectors.json").read_text("utf-8"))


@pytest.mark.parametrize("vec", VECTORS["actions"], ids=lambda v: v["kind"])
def test_action_golden_vectors(vec):
    action = build_action(vec["kind"], **vec["fields"])
    assert action == vec["action"]
    assert canonical(action).decode("utf-8") == vec["canonical"]
    # Hashes are recomputed from the stored canonical string, independently
    # of the implementation, so other languages can check against the file.
    digest = hashlib.sha256(vec["canonical"].encode("utf-8")).digest()
    assert action_hash(action) == vec["action_hash"] == "0x" + digest.hex()
    nonce = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert action_nonce(action) == vec["action_nonce"] == nonce
    assert len(nonce) == 43


@pytest.mark.parametrize("vec", VECTORS["sows"])
def test_sow_golden_vectors(vec):
    assert canonical(vec["sow"]).decode("utf-8") == vec["canonical"]
    assert "日本語" in vec["canonical"]  # ensure_ascii=False
    assert sow_hash(vec["sow"]) == vec["sow_hash"]
    assert vec["sow_hash"] == "0x" + hashlib.sha256(vec["canonical"].encode("utf-8")).hexdigest()


def test_canonical_form():
    assert canonical({"b": 1, "a": [True, None, "é"]}) == '{"a":[true,null,"é"],"b":1}'.encode()


@pytest.mark.parametrize("obj", [1.5, {"amount": 25.0}, [1, [2.0]], {1: "int key"}, {"x": {1, 2}}])
def test_canonical_rejects_ambiguous_values(obj):
    with pytest.raises(TypeError):
        canonical(obj)


def _base(**extra):
    return dict(approval_id="APR-X", exp=1790000000, **extra)


def test_build_action_rejects_bad_fields():
    with pytest.raises(TypeError):
        build_action("engagement.fund", **_base(amount_micro=25.0))
    with pytest.raises(TypeError):
        build_action("engagement.fund", **_base(amount=1))
    with pytest.raises(ValueError):
        build_action("wire.transfer", **_base())
    with pytest.raises(ValueError):
        build_action("session.login", exp=1)  # no approval_id
    with pytest.raises(ValueError):
        build_action("engagement.fund", **_base(payee_address="0x123"))
    with pytest.raises(ValueError):
        build_action("engagement.fund", **_base(engagement_id="ORD-1"))
    with pytest.raises(AgentIdError):
        build_action("engagement.fund", **_base(payee_agent_id="AGT-0000-0000-1"))


def test_none_fields_are_omitted_and_each_approval_hashes_differently():
    a = build_action("session.login", **_base(engagement_id=None))
    assert set(a) == {"v", "kind", "approval_id", "exp"}
    b = build_action("session.login", approval_id="APR-Y", exp=1790000000)
    assert action_hash(a) != action_hash(b)


def test_describe_rows():
    rows = dict(describe(VECTORS["actions"][1]["action"]))
    assert rows["Action"] == "Release milestone"
    assert rows["Amount"] == "15 USDC"
    assert rows["Milestone"] == "#2"
    assert rows["Risk screening"].endswith("(warning acknowledged)")
    assert rows["Expires"].endswith("UTC")
    assert format_usdc(1) == "0.000001 USDC"
    assert format_usdc(1_234_500_000) == "1,234.50 USDC"


def test_executor_registry():
    kind = "test.noop"
    try:
        @executor(kind)
        def run(approval, action):
            return ExecutionResult(ok=True, summary="done")

        assert get_executor(kind) is run
        assert run(None, {}).ledger_ids == [] and run(None, {}).redirect is None
        with pytest.raises(ValueError):
            executor(kind)(lambda a, b: None)
    finally:
        EXECUTORS.pop(kind, None)
    with pytest.raises(LookupError):
        get_executor(kind)
