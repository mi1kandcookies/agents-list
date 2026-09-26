"""The kit against the platform it plugs into: the same canonical JSON, the
evidence hash the submit endpoint returns, and operator fields the stamped
manifest accepts. agentkit itself never imports app/; only this test does."""
import copy
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit.evidence import canonical_json, evidence_hash, platform_evidence
from agentkit.manifest import load_manifest, operator_fields, task_price_micro
from agentkit.types import Artifact, CheckResult, HumanReview, Submission, Usage

FIXTURE = Path(__file__).parent / "fixtures" / "example_specialist"


def test_the_kit_imports_no_platform_code_and_no_provider_sdk():
    code = ("import sys, agentkit, agentkit.__main__, agentkit.evals; "
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] in ('app', 'chain', 'flask', 'anthropic', 'openai')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[2])
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize("obj", [
    {"b": 1, "a": "é", "c": [True, None, -3, {"z": "\u2028", "\U00010000": "\x01"}], "｡": ""},
    [], {}, "plain", 0, 10**30,
])
def test_canonical_json_is_the_platforms(obj):
    from app.approvals.actions import canonical
    assert canonical_json(obj) == canonical(obj)


@pytest.mark.parametrize("obj", [1.5, {"x": 0.0}, {1: "x"}])
def test_canonical_json_rejects_what_the_platform_rejects(obj):
    from app.approvals.actions import canonical
    with pytest.raises(TypeError):
        canonical(obj)
    with pytest.raises(TypeError):
        canonical_json(obj)


def _funded_engagement(db, agent_id, milestones=2):
    from app.models import Engagement, Milestone
    eng = Engagement(agent_id=agent_id, outcome="Write a market brief", status="funded",
                     total_micro=milestones * 5_000_000)
    for idx in range(milestones):
        eng.milestones.append(Milestone(idx=idx, title=f"Part {idx}", acceptance="Done",
                                        amount_micro=5_000_000, status="funded"))
    db.session.add(eng)
    db.session.commit()
    return eng.id


def _submission(engagement_id, idx, artifacts=1):
    return Submission(
        engagement_id=engagement_id, milestone_id="m1-brief", status="ready_for_review",
        summary="Brief written — 2 cited figures",
        artifacts=[Artifact(path=f"deliverables/m1-brief/part-{i:03}-brief.md", sha256="ab" * 32,
                            bytes=1200 + i, media_type="text/markdown") for i in range(artifacts)],
        check_results=[CheckResult(check="file_exists", passed=True),
                       CheckResult(check="rubric_grader", passed=True, kind="rubric", score=0.8125),
                       CheckResult(check="human_signoff", passed=None, kind="human")],
        human_review=HumanReview(required=True, reviewer_role="editor"),
        usage=Usage(1200, 300, cost_usd=0.042), milestone_idx=idx)


@pytest.mark.parametrize("artifacts", [1, 80])   # 80 artifacts: the digested form
def test_submit_endpoint_returns_our_evidence_hash(client, db, agent, artifacts):
    eid = _funded_engagement(db, agent)
    sub = _submission(eid, 1, artifacts)
    sub.evidence_hash = evidence_hash(sub)
    evidence = platform_evidence(sub)
    assert len(evidence) <= 4000 and ("artifacts_sha256" in evidence) == (artifacts == 80)
    resp = client.post(f"/api/engagements/{eid}/milestones/1/submit", json={"evidence": evidence})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["evidence_hash"] == sub.evidence_hash
    assert resp.get_json()["milestone"]["status"] == "submitted"


def test_operator_fields_fit_the_stamped_manifest(db, agent):
    from app.models import Agent
    from app.seller.stamp import build_manifest, manifest_hash

    spec = load_manifest(FIXTURE)
    spec.listing.pricing.task_price_usdc = 4.1              # a float that * 10**6 truncates
    fields = operator_fields(spec)
    platform = {k: v for k, v in fields.items() if k != "spec_hash"}
    row = db.session.get(Agent, agent)
    price = task_price_micro(spec)
    manifest = build_manifest(row, **copy.deepcopy(platform), price_min_micro=price,
                              price_max_micro=price, payout_address="0x" + "c" * 40)
    for key in ("model", "tools", "skills", "mcp_servers"):
        assert manifest[key] == fields[key], key          # stored exactly as we give them
    assert manifest["price_min_micro"] == manifest["price_max_micro"] == 4_100_000
    assert manifest_hash(manifest).startswith("0x")
