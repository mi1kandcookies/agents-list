"""Mandate tokens and service (docs/decisions/0001-custody-chain.md §5)."""
from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.common.agent_ids import from_db_id
from app.mandates import service, tokens
from app.mandates.service import MandateError

AGENT_A = from_db_id(101)
AGENT_B = from_db_id(102)
AGENT_C = from_db_id(103)
AGENT_D = from_db_id(104)
BUDGET = 25_000_000
PER_TX = 10_000_000


@pytest.fixture()
def signing_pem(app):
    """A generated ES256 key for this test only (never the instance key)."""
    pem = tokens.generate_pem()
    app.config["MANDATE_SIGNING_KEY"] = pem
    return pem


def _exp(seconds: int = 3600) -> int:
    return int(time.time()) + seconds


def _approval(db, eng, *, kind="engagement.fund", state="consumed", amount=BUDGET, sub=None):
    from app.approvals.actions import action_hash, action_nonce, build_action, canonical
    from app.common.ids import new_id
    from app.models import Approval
    apr_id = new_id("APR")
    action = build_action(kind, approval_id=apr_id, exp=_exp(180), engagement_id=eng.id,
                          amount_micro=amount)
    row = Approval(id=apr_id, kind=kind, action_json=canonical(action).decode(),
                   action_hash=action_hash(action), nonce=action_nonce(action), flow="device",
                   state=state, expires_at=datetime.now(timezone.utc) + timedelta(minutes=3),
                   engagement_id=eng.id, human_sub=sub,
                   consumed_at=datetime.now(timezone.utc) if state == "consumed" else None)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture()
def engagement(db, agent, human):
    from app.models import Engagement
    eng = Engagement(agent_id=agent, buyer_human_id=human.id, outcome="Build it",
                     category="Development", total_micro=BUDGET, sow_hash="0x" + "ab" * 32,
                     status="funded")
    db.session.add(eng)
    db.session.commit()
    return eng


@pytest.fixture()
def root(db, signing_pem, engagement, human):
    approval = _approval(db, engagement, sub=human.world_sub)
    return service.issue_root(engagement, human, approval, AGENT_A, BUDGET,
                              ["Development", "Design"], 2, PER_TX, _exp())


def _code(excinfo) -> str:
    return excinfo.value.code


# ── Tokens ───────────────────────────────────────────────────────────────────

def test_root_token_header_and_claims(root, human, engagement):
    header = jwt.get_unverified_header(root.token)
    key = tokens.signing_key()
    assert header == {"alg": "ES256", "typ": "mandate+jwt", "kid": tokens.key_thumbprint(key.public_key())}
    claims = service.verify_chain(root.token)
    assert claims["jti"] == root.id and root.id.startswith("MND-")
    assert claims["iss"] == "agents-list" and claims["sub"] == AGENT_A
    assert claims["root"] == root.id and claims["par"] is None and claims["dep"] == 0
    assert claims["apr"] == root.approval_id and claims["eng"] == engagement.id
    assert claims["sow"] == engagement.sow_hash
    assert claims["cap"] == {"budget_micro": BUDGET, "categories": ["Development", "Design"],
                             "max_depth": 2, "per_tx_max_micro": PER_TX, "payees": None}
    assert claims["iat"] == claims["nbf"] and claims["exp"] > claims["iat"]


def test_hum_is_sha256_of_sub_and_never_the_raw_sub(root, human):
    import hashlib
    claims = service.verify_chain(root.token)
    assert claims["hum"] == hashlib.sha256(human.world_sub.encode()).hexdigest()
    payload = base64.urlsafe_b64decode(root.token.split(".")[1] + "==").decode()
    assert human.world_sub not in payload
    assert human.world_sub[2:] not in payload  # not even without the 0x prefix


def test_kid_is_rfc7638_thumbprint():
    from jwt.algorithms import ECAlgorithm
    key = jwt.api_jwk.PyJWK.from_dict(json.loads(ECAlgorithm.to_jwk(
        tokens._load_pem(tokens.generate_pem()).public_key())))
    import hashlib
    jwk = {k: key._jwk_data[k] for k in ("crv", "kty", "x", "y")}
    expected = base64.urlsafe_b64encode(hashlib.sha256(
        json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()).digest()).rstrip(b"=").decode()
    assert tokens.key_thumbprint(key.key) == expected


def test_tampered_signature_fails(root):
    head, payload, sig = root.token.split(".")
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    with pytest.raises(MandateError) as exc:
        service.verify_chain(f"{head}.{payload}.{flipped}")
    assert _code(exc) == "MANDATE_INVALID"


def test_tampered_payload_fails(root):
    head, payload, sig = root.token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["cap"]["budget_micro"] *= 10
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(MandateError) as exc:
        service.verify_chain(f"{head}.{forged}.{sig}")
    assert _code(exc) == "MANDATE_INVALID"


def test_token_signed_by_another_key_fails(root, app):
    other = tokens._load_pem(tokens.generate_pem())
    claims = tokens.decode(root.token)
    with pytest.raises(MandateError) as exc:
        service.verify_chain(tokens.encode(claims, key=other))
    assert _code(exc) == "MANDATE_INVALID"


def test_validly_signed_token_not_on_record_fails(root):
    claims = tokens.decode(root.token)
    claims["cap"]["budget_micro"] = 1  # re-signed with the real key, but not the stored token
    with pytest.raises(MandateError) as exc:
        service.verify_chain(tokens.encode(claims))
    assert _code(exc) == "MANDATE_INVALID"


def test_dev_key_is_generated_once_into_instance(app, tmp_path, monkeypatch):
    monkeypatch.delenv("MANDATE_SIGNING_KEY", raising=False)
    app.config.pop("MANDATE_SIGNING_KEY", None)
    monkeypatch.setattr(app, "instance_path", str(tmp_path))
    first = tokens.signing_key()
    path = tmp_path / tokens.KEY_FILENAME
    assert path.exists() and (path.stat().st_mode & 0o777) == 0o600
    assert tokens.key_thumbprint(tokens.signing_key().public_key()) == \
        tokens.key_thumbprint(first.public_key())


def test_production_requires_configured_key(app, monkeypatch):
    monkeypatch.delenv("MANDATE_SIGNING_KEY", raising=False)
    app.config.pop("MANDATE_SIGNING_KEY", None)
    monkeypatch.setitem(app.config, "ENV_NAME", "production")
    with pytest.raises(RuntimeError):
        tokens.signing_key()


# ── Root issuance ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,state", [("engagement.fund", "approved"),
                                         ("engagement.fund", "pending"),
                                         ("milestone.release", "consumed")])
def test_root_needs_consumed_fund_approval(db, signing_pem, engagement, human, kind, state):
    approval = _approval(db, engagement, kind=kind, state=state)
    with pytest.raises(MandateError) as exc:
        service.issue_root(engagement, human, approval, AGENT_A, BUDGET, ["Development"], 2,
                           PER_TX, _exp())
    assert _code(exc) == "MANDATE_INVALID"


def test_root_budget_cannot_exceed_approved_amount(db, signing_pem, engagement, human):
    approval = _approval(db, engagement, amount=BUDGET)
    with pytest.raises(MandateError) as exc:
        service.issue_root(engagement, human, approval, AGENT_A, BUDGET + 1, ["Development"], 2,
                           PER_TX, _exp())
    assert _code(exc) == "MANDATE_EXCEEDED"


def test_root_max_depth_capped_by_config(db, signing_pem, engagement, human):
    approval = _approval(db, engagement)
    with pytest.raises(MandateError) as exc:
        service.issue_root(engagement, human, approval, AGENT_A, BUDGET, ["Development"], 3,
                           PER_TX, _exp())
    assert _code(exc) == "DEPTH_EXCEEDED"


def test_root_refused_for_banned_human(db, signing_pem, engagement, human):
    approval = _approval(db, engagement)
    human.banned_at = datetime.now(timezone.utc)
    db.session.commit()
    with pytest.raises(MandateError) as exc:
        service.issue_root(engagement, human, approval, AGENT_A, BUDGET, ["Development"], 2,
                           PER_TX, _exp())
    assert _code(exc) == "BANNED"


def test_root_refused_for_other_human(db, signing_pem, engagement, human):
    from app.models import Human
    other = Human(world_sub="0x" + "6" * 64)
    db.session.add(other)
    db.session.commit()
    approval = _approval(db, engagement, sub=human.world_sub)
    with pytest.raises(MandateError) as exc:
        service.issue_root(engagement, other, approval, AGENT_A, BUDGET, ["Development"], 2,
                           PER_TX, _exp())
    assert _code(exc) == "MANDATE_INVALID"


def test_root_with_past_exp_is_expired(db, signing_pem, engagement, human):
    approval = _approval(db, engagement)
    with pytest.raises(MandateError) as exc:
        service.issue_root(engagement, human, approval, AGENT_A, BUDGET, ["Development"], 2,
                           PER_TX, int(time.time()) - 1)
    assert _code(exc) == "MANDATE_EXPIRED"


# ── Attenuation ──────────────────────────────────────────────────────────────

def test_child_narrows_and_verifies(root):
    child = service.attenuate(root.token, AGENT_B, 10_000_000, ["Development"], _exp(1800))
    claims = service.verify_chain(child.token)
    assert claims["par"] == root.id and claims["root"] == root.id and claims["dep"] == 1
    assert claims["apr"] == root.approval_id and claims["hum"] == tokens.decode(root.token)["hum"]
    assert claims["cap"]["per_tx_max_micro"] == PER_TX
    assert claims["cap"]["max_depth"] == 2
    assert child.engagement_id == root.engagement_id and child.approval_id is None


@pytest.mark.parametrize("kwargs,code", [
    ({"budget_micro": BUDGET + 1}, "MANDATE_EXCEEDED"),
    ({"categories": ["Development", "Marketing"]}, "CATEGORY_NOT_ALLOWED"),
    ({"exp_delta": 7200}, "MANDATE_EXCEEDED"),
    ({"per_tx_max_micro": PER_TX + 1}, "MANDATE_EXCEEDED"),
    ({"budget_micro": 0}, "MANDATE_INVALID"),
    ({"categories": []}, "MANDATE_INVALID"),
    ({"exp_delta": -10}, "MANDATE_EXPIRED"),
    ({"agent": "AGT-NOPE"}, "MANDATE_INVALID"),
])
def test_attenuation_violations(root, kwargs, code):
    exp = _exp(kwargs.pop("exp_delta", 1800)) if "exp_delta" in kwargs else _exp(1800)
    args = {"budget_micro": 5_000_000, "categories": ["Development"], "exp": exp,
            "per_tx_max_micro": None}
    agent = kwargs.pop("agent", AGENT_B)
    args.update(kwargs)
    with pytest.raises(MandateError) as exc:
        service.attenuate(root.token, agent, **args)
    assert _code(exc) == code


def test_depth_limit(root):
    child = service.attenuate(root.token, AGENT_B, 10_000_000, ["Development"], _exp(1800))
    grandchild = service.attenuate(child.token, AGENT_C, 5_000_000, ["Development"], _exp(1700))
    assert grandchild.depth == 2
    with pytest.raises(MandateError) as exc:
        service.attenuate(grandchild.token, AGENT_D, 1_000_000, ["Development"], _exp(1600))
    assert _code(exc) == "DEPTH_EXCEEDED"


def test_depth_limit_follows_root_max_depth(db, signing_pem, engagement, human):
    approval = _approval(db, engagement)
    root = service.issue_root(engagement, human, approval, AGENT_A, BUDGET, ["Development"], 1,
                              PER_TX, _exp())
    child = service.attenuate(root.token, AGENT_B, 1_000_000, ["Development"], _exp(1800))
    with pytest.raises(MandateError) as exc:
        service.attenuate(child.token, AGENT_C, 500_000, ["Development"], _exp(1700))
    assert _code(exc) == "DEPTH_EXCEEDED"


def test_sibling_budget_accounting(db, root):
    a = service.attenuate(root.token, AGENT_B, 10_000_000, ["Development"], _exp(1800))
    service.attenuate(root.token, AGENT_C, 10_000_000, ["Development"], _exp(1800))
    service.spend(root.id, 3_000_000)
    # 25 − 3 spent − 20 reserved by active siblings = 2 left
    with pytest.raises(MandateError) as exc:
        service.attenuate(root.token, AGENT_D, 2_000_001, ["Development"], _exp(1800))
    assert _code(exc) == "MANDATE_EXCEEDED"
    service.attenuate(root.token, AGENT_D, 2_000_000, ["Development"], _exp(1800))
    # The parent itself cannot spend budget reserved by its children.
    with pytest.raises(MandateError) as exc:
        service.spend(root.id, 1)
    assert _code(exc) == "MANDATE_EXCEEDED"
    # Revoking a child releases its unspent reservation; what it spent stays committed.
    service.spend(a.id, 4_000_000)
    service.revoke(a.id)
    assert service.remaining_micro(db.session.get(type(root), root.id)) == 6_000_000
    service.attenuate(root.token, AGENT_B, 6_000_000, ["Development"], _exp(1800))


def test_revoked_or_expired_ancestor_blocks_attenuation_and_spend(db, root):
    child = service.attenuate(root.token, AGENT_B, 10_000_000, ["Development"], _exp(1800))
    root.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    for call in (lambda: service.attenuate(child.token, AGENT_C, 1, ["Development"], _exp(60)),
                 lambda: service.spend(child.id, 1),
                 lambda: service.verify_chain(child.token)):
        with pytest.raises(MandateError) as exc:
            call()
        assert _code(exc) == "MANDATE_EXPIRED"


def test_expired_token_is_expired(db, root):
    child = service.attenuate(root.token, AGENT_B, 1_000_000, ["Development"], _exp(1800))
    claims = tokens.decode(child.token)
    claims["exp"] = int(time.time()) - 5
    expired = tokens.encode(claims)
    child.token = expired
    db.session.commit()
    with pytest.raises(MandateError) as exc:
        service.verify_chain(expired)
    assert _code(exc) == "MANDATE_EXPIRED"


def test_banned_human_blocks_whole_chain(db, root, human):
    child = service.attenuate(root.token, AGENT_B, 1_000_000, ["Development"], _exp(1800))
    human.banned_at = datetime.now(timezone.utc)
    db.session.commit()
    for call in (lambda: service.verify_chain(child.token),
                 lambda: service.attenuate(child.token, AGENT_C, 1, ["Development"], _exp(60)),
                 lambda: service.spend(child.id, 1)):
        with pytest.raises(MandateError) as exc:
            call()
        assert _code(exc) == "BANNED"


def test_widened_record_fails_chain_recheck(db, root):
    child = service.attenuate(root.token, AGENT_B, 1_000_000, ["Development"], _exp(1800))
    child.budget_micro = BUDGET * 2  # row edited behind the token's back
    db.session.commit()
    with pytest.raises(MandateError) as exc:
        service.verify_chain(child.token)
    assert _code(exc) == "MANDATE_INVALID"


# ── Spend ────────────────────────────────────────────────────────────────────

def test_spend_checks_per_tx_category_and_budget(db, root):
    with pytest.raises(MandateError) as exc:
        service.spend(root.id, PER_TX + 1)
    assert _code(exc) == "MANDATE_EXCEEDED"
    with pytest.raises(MandateError) as exc:
        service.spend(root.id, 1, category="Marketing")
    assert _code(exc) == "CATEGORY_NOT_ALLOWED"
    service.spend(root.id, PER_TX, category="Development")
    service.spend(root.id, PER_TX)
    with pytest.raises(MandateError) as exc:
        service.spend(root.id, BUDGET - 2 * PER_TX + 1)
    assert _code(exc) == "MANDATE_EXCEEDED"
    assert db.session.get(type(root), root.id).spent_micro == 2 * PER_TX
    with pytest.raises(MandateError) as exc:
        service.spend("MND-000000000000", 1)
    assert _code(exc) == "MANDATE_INVALID"


def test_concurrent_spends_cannot_overspend(tmp_path):
    """Two spends that together exceed the budget race; exactly one wins."""
    from app import create_app
    from app.extensions import db as _db
    from app.models import Agent, Engagement, Human, Mandate

    app = create_app("testing", SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'race.db'}",
                     SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15}},
                     MANDATE_SIGNING_KEY=tokens.generate_pem())
    with app.app_context():
        _db.create_all()
        agent = Agent(name="A", description="", category="Development", billing="per_minute",
                      min_price=0, max_price=0, current_price=0, seller="0x" + "b" * 40,
                      deployer_wallet="0x" + "b" * 40)
        human = Human(world_sub="0x" + "7" * 64)
        _db.session.add_all([agent, human])
        _db.session.commit()
        eng = Engagement(agent_id=agent.id, buyer_human_id=human.id, total_micro=BUDGET)
        _db.session.add(eng)
        _db.session.commit()
        approval = _approval(_db, eng)
        root = service.issue_root(eng, human, approval, AGENT_A, 10_000_000, ["Development"], 2,
                                  10_000_000, _exp())
        root_id = root.id

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker():
        with app.app_context():
            barrier.wait()
            try:
                service.spend(root_id, 6_000_000)
                results.append("ok")
            except MandateError as exc:
                results.append(exc.code)
            finally:
                _db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == ["MANDATE_EXCEEDED", "ok"]
    with app.app_context():
        assert _db.session.get(Mandate, root_id).spent_micro == 6_000_000
        _db.drop_all()


# ── Revoke ───────────────────────────────────────────────────────────────────

def test_revoke_cascades_to_descendants(db, root):
    a = service.attenuate(root.token, AGENT_B, 10_000_000, ["Development"], _exp(1800))
    a1 = service.attenuate(a.token, AGENT_C, 2_000_000, ["Development"], _exp(1700))
    b = service.attenuate(root.token, AGENT_D, 5_000_000, ["Design"], _exp(1800))
    assert sorted(service.revoke(a.id)) == sorted([a.id, a1.id])
    for token in (a.token, a1.token):
        with pytest.raises(MandateError) as exc:
            service.verify_chain(token)
        assert _code(exc) == "MANDATE_REVOKED"
    service.verify_chain(b.token)  # sibling branch untouched
    service.verify_chain(root.token)
    assert sorted(service.revoke(root.id)) == sorted([root.id, b.id])
    assert service.revoke(root.id) == []
    with pytest.raises(MandateError) as exc:
        service.spend(b.id, 1)
    assert _code(exc) == "MANDATE_REVOKED"


# ── Graph and routes ─────────────────────────────────────────────────────────

def test_chain_graph_shape(db, root, human, engagement):
    import hashlib
    a = service.attenuate(root.token, AGENT_B, 10_000_000, ["Development"], _exp(1800))
    a1 = service.attenuate(a.token, AGENT_C, 2_000_000, ["Development"], _exp(1700))
    service.spend(a1.id, 1_000_000)
    graph = service.chain_graph(engagement.id)
    assert set(graph) == {"engagement_id", "nodes", "edges"}
    by_id = {n["id"]: n for n in graph["nodes"]}
    humans = [n for n in graph["nodes"] if n["type"] == "human"]
    mandates = [n for n in graph["nodes"] if n["type"] == "mandate"]
    agents = {n["id"] for n in graph["nodes"] if n["type"] == "agent"}
    assert len(humans) == 1 and humans[0]["hum"] == hashlib.sha256(human.world_sub.encode()).hexdigest()
    assert {m["id"] for m in mandates} == {root.id, a.id, a1.id}
    assert agents == {AGENT_A, AGENT_B, AGENT_C}
    node = by_id[a1.id]
    assert node["budget_micro"] == 2_000_000 and node["spent_micro"] == 1_000_000
    assert node["categories"] == ["Development"] and node["depth"] == 2
    assert isinstance(node["exp"], int) and node["status"] == "active"
    edges = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}
    assert (humans[0]["id"], root.id, "approved") in edges
    assert (root.id, a.id, "attenuates") in edges and (a.id, a1.id, "attenuates") in edges
    assert (a1.id, AGENT_C, "grants") in edges
    assert human.world_sub not in json.dumps(graph)
    assert human.world_sub[2:] not in json.dumps(graph)
    assert service.chain_graph("ENG-000000000000") == {"engagement_id": "ENG-000000000000",
                                                       "nodes": [], "edges": []}


def test_mandate_route(client, root, human):
    body = client.get(f"/api/mandates/{root.id}").get_json()
    assert body["mandate_id"] == root.id and body["status"] == "active"
    assert body["chain_valid"] is True and body["claims"]["cap"]["budget_micro"] == BUDGET
    assert body["remaining_micro"] == BUDGET and human.world_sub not in json.dumps(body)
    service.revoke(root.id)
    body = client.get(f"/api/mandates/{root.id}").get_json()
    assert body["status"] == "revoked" and body["chain_error"] == "MANDATE_REVOKED"
    missing = client.get("/api/mandates/MND-000000000000")
    assert missing.status_code == 404 and missing.get_json()["code"] == "MANDATE_INVALID"


def test_chain_route(client, root, engagement, human):
    body = client.get(f"/api/engagements/{engagement.id}/chain").get_json()
    assert {n["id"] for n in body["nodes"] if n["type"] == "mandate"} == {root.id}
    assert human.world_sub not in json.dumps(body)
    assert client.get("/api/engagements/ENG-000000000000/chain").status_code == 404
