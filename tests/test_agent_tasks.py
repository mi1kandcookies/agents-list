"""POST /api/agents/<AGT>/tasks: x402 v2 402 → X-PAYMENT + mandate → 200,
in simulated escrow mode, with the fake screener."""
from __future__ import annotations

import json

import pytest
from eth_account import Account

from app.mandates import service as mandates
from chain import x402_v2
from chain.x402_official import create_payment_payload, payment_signature_header
from chain.x402_v2 import Expectation, PaymentRequirements
from tests.conftest import WALLET
from tests.test_mandates import _approval, _exp, signing_pem  # noqa: F401  (fixture)

PAYEE = WALLET.lower()
PRICE = 50_000                 # the stamped manifest's price_min_micro (min_price 0.05)
BUDGET = 1_000_000
PER_TX = 200_000


@pytest.fixture()
def screener(app, fake_screener):
    app.extensions["screener"] = fake_screener
    return fake_screener


@pytest.fixture()
def callee(db, agent):
    """The test agent, operator-stamped (price_min 0.05 USDC) so it is hireable."""
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    row = db.session.get(Agent, agent)
    dev_stamp(row)
    db.session.commit()
    return row


def _restamp(db, agent, **manifest_fields):
    from app.seller.stamp import dev_stamp
    agent.manifest_json = json.dumps({**json.loads(agent.manifest_json), **manifest_fields})
    dev_stamp(agent)
    db.session.commit()


@pytest.fixture()
def payer_key():
    return Account.create()


@pytest.fixture()
def payer_agent(db, payer_key):
    from app.models import Agent
    row = Agent(name="Orchestrator", description="Hires helpers", category="Development",
                billing="per_minute", seller=payer_key.address, deployer_wallet=payer_key.address)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture()
def mandate(db, signing_pem, human, payer_agent):
    """Root mandate granted to the paying agent from a consumed fund approval."""
    from app.models import Engagement
    eng = Engagement(agent_id=payer_agent.id, buyer_human_id=human.id, outcome="Coordinate",
                     category="Development", total_micro=BUDGET, sow_hash="0x" + "ab" * 32,
                     status="funded")
    db.session.add(eng)
    db.session.commit()
    apr = _approval(db, eng, amount=BUDGET, sub=human.world_sub)
    return mandates.issue_root(eng, human, apr, payer_agent.public_id, BUDGET, ["Development"],
                               2, PER_TX, _exp())


def _url(agent):
    return f"/api/agents/{agent.public_id}/tasks"


def _challenge(client, callee):
    resp = client.post(_url(callee), json={"task": "Summarize the release notes"})
    assert resp.status_code == 402, resp.get_json()
    return resp


def _pay(client, callee, key, mandate_row, *, headers=None, amount=PRICE, mutate=None):
    body = _challenge(client, callee).get_json()
    req = PaymentRequirements.from_dict(body["accepts"][0])
    payload = x402_v2.sign_payment(key, req, expected=Expectation(pay_to=PAYEE, amount_micro=amount))
    if mutate:
        mutate(payload)
    h = {"X-PAYMENT": x402_v2.encode_header(payload)}
    if mandate_row is not None:
        h["Authorization"] = f"Mandate {mandate_row.token}"
    h.update(headers or {})
    return client.post(_url(callee), json={"task": "Summarize the release notes"}, headers=h), payload


def _spent(db, mandate_row):
    db.session.expire_all()
    return db.session.get(type(mandate_row), mandate_row.id).spent_micro


def _ledger(eng_id):
    from app.models import LedgerEntry
    return LedgerEntry.query.filter_by(engagement_id=eng_id).all()


# ── 402 ──────────────────────────────────────────────────────────────────────
def test_unpaid_request_gets_v2_requirements(client, callee):
    resp = _challenge(client, callee)
    body = resp.get_json()
    assert body["x402Version"] == 2 and body["resource"]["url"] == _url(callee)
    req = body["accepts"][0]
    assert req["scheme"] == "exact" and req["network"] == "eip155:11155111"
    assert req["amount"] == str(PRICE) and req["payTo"] == PAYEE
    assert req["extra"] == {"name": "USDC", "version": "2"}
    assert x402_v2.decode_header(resp.headers["PAYMENT-REQUIRED"]) == body


def test_price_defaults_without_a_stamped_price(client, db, app, callee):
    _restamp(db, callee, price_min_micro=0)
    app.config["AGENT_TASK_PRICE_USDC"] = "0.25"
    assert _challenge(client, callee).get_json()["accepts"][0]["amount"] == "250000"


def test_unstamped_agent_cannot_take_paid_tasks(client, db, agent):
    from app.models import Agent
    row = db.session.get(Agent, agent)
    resp = client.post(_url(row), json={"task": "t"})
    assert resp.status_code == 409 and resp.get_json()["code"] == "NOT_STAMPED"


def test_task_required(client, callee):
    resp = client.post(_url(callee), json={})
    assert resp.status_code == 400 and resp.get_json()["field"] == "task"


# ── 200 ──────────────────────────────────────────────────────────────────────
def test_paid_task_settles_under_the_mandate(client, db, callee, payer_agent, payer_key,
                                             mandate, screener):
    resp, payload = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["status"] == "accepted" and body["agent_id"] == callee.public_id
    assert body["payer_agent_id"] == payer_agent.public_id and body["mandate_id"] == mandate.id
    pay = body["payment"]
    assert pay["simulated"] is True and pay["tx_hash"].startswith("sim-")
    assert pay["amount_micro"] == PRICE and pay["to"] == PAYEE and pay["payee_source"] == "profile"
    assert pay["from"] == payer_key.address.lower()
    # The specialist screens the payer before accepting the sub-hire, then
    # screens its own payee hop and exact authorization.
    assert [c["hop"] for c in screener.calls] == ["payer.check", "subhire.hop"]
    assert screener.calls[0]["chain_address"] == payer_key.address.lower()
    assert screener.calls[1]["chain_address"] == PAYEE
    assert _spent(db, mandate) == PRICE
    (entry,) = _ledger(mandate.engagement_id)
    assert entry.id == body["receipt_id"] and entry.kind == "subhire_alloc"
    assert entry.approval_id == mandate.approval_id and entry.screening_id == body["screening"]["id"]
    assert body["payer_screening"]["hop"] == "payer.check"
    settle = x402_v2.decode_header(resp.headers["PAYMENT-RESPONSE"])
    assert settle == {"success": True, "transaction": pay["tx_hash"], "network": "eip155:11155111",
                      "payer": payer_key.address.lower()}
    assert resp.headers["X-PAYMENT-RESPONSE"] == resp.headers["PAYMENT-RESPONSE"]


def test_official_x402_client_payload_is_accepted_by_task_route(
        client, db, callee, payer_agent, payer_key, mandate, screener):
    """The protected route accepts the official SDK's v2 exact payload."""
    body = _challenge(client, callee).get_json()
    req = PaymentRequirements.from_dict(body["accepts"][0])
    payload = create_payment_payload(
        body,
        payer_key,
        expected=Expectation(pay_to=PAYEE, amount_micro=PRICE, asset=req.asset),
        domain=None,
    )
    response = client.post(
        _url(callee),
        json={"task": "Summarize the release notes"},
        headers={
            "PAYMENT-SIGNATURE": payment_signature_header(payload),
            "Authorization": f"Mandate {mandate.token}",
        },
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["payment"]["amount_micro"] == PRICE


def test_payment_signature_header_is_accepted(client, db, callee, payer_key, mandate, screener):
    body = _challenge(client, callee).get_json()
    req = PaymentRequirements.from_dict(body["accepts"][0])
    payload = x402_v2.sign_payment(payer_key, req, expected=Expectation(pay_to=PAYEE, amount_micro=PRICE))
    resp = client.post(_url(callee), json={"task": "t"}, headers={
        "PAYMENT-SIGNATURE": x402_v2.encode_header(payload),
        "Authorization": f"Mandate {mandate.token}"})
    assert resp.status_code == 200, resp.get_json()


# ── refusals ─────────────────────────────────────────────────────────────────
def test_replayed_payment_is_refused(client, db, callee, payer_key, mandate, screener):
    first, payload = _pay(client, callee, payer_key, mandate)
    assert first.status_code == 200
    again = client.post(_url(callee), json={"task": "again"}, headers={
        "X-PAYMENT": x402_v2.encode_header(payload), "Authorization": f"Mandate {mandate.token}"})
    assert again.status_code == 409 and again.get_json()["code"] == "DUPLICATE_NONCE"
    assert _spent(db, mandate) == PRICE and len(_ledger(mandate.engagement_id)) == 1


def test_mandate_required(client, db, callee, payer_key, mandate, screener):
    resp, _ = _pay(client, callee, payer_key, None)
    assert resp.status_code == 401 and resp.get_json()["code"] == "MANDATE_INVALID"
    resp, _ = _pay(client, callee, payer_key, None, headers={"Authorization": "Mandate nope"})
    assert resp.status_code == 403 and resp.get_json()["code"] == "MANDATE_INVALID"
    assert _spent(db, mandate) == 0 and screener.calls == []


def test_revoked_mandate_refused(client, db, callee, payer_key, mandate, screener):
    mandates.revoke(mandate.id)
    resp, _ = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 403 and resp.get_json()["code"] == "MANDATE_REVOKED"


def test_payer_must_be_the_mandates_agent(client, db, callee, mandate, screener):
    resp, _ = _pay(client, callee, Account.create(), mandate)
    assert resp.status_code == 403 and resp.get_json()["code"] == "PAYER_NOT_MANDATED"
    assert _spent(db, mandate) == 0


def test_tampered_payment_gets_402(client, db, callee, payer_key, mandate, screener):
    def bump(p):
        p["payload"]["authorization"]["value"] = str(PRICE - 1)
    resp, _ = _pay(client, callee, payer_key, mandate, mutate=bump)
    assert resp.status_code == 402 and resp.get_json()["code"] == "AMOUNT_MISMATCH"
    assert resp.get_json()["accepts"][0]["amount"] == str(PRICE)
    assert _spent(db, mandate) == 0


def test_screening_refusal_fails_closed(client, db, callee, payer_key, mandate, screener):
    screener.set(PAYEE, "REFUSE")
    resp, payload = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"
    assert resp.get_json()["screening"]["verdict"] == "REFUSE"
    assert _spent(db, mandate) == 0 and _ledger(mandate.engagement_id) == []
    screener.set(PAYEE, "ASK_HUMAN")
    resp, _ = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"


def test_payer_screening_refusal_stops_before_payee_screen(client, db, callee, payer_key,
                                                            mandate, screener):
    screener.set(payer_key.address, "REFUSE")
    resp, _ = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"
    assert resp.get_json()["screening"]["hop"] == "payer.check"
    assert [call["hop"] for call in screener.calls] == ["payer.check"]
    assert _spent(db, mandate) == 0 and _ledger(mandate.engagement_id) == []


def test_amount_over_per_tx_cap_refused(client, db, app, callee, payer_key, mandate, screener):
    _restamp(db, callee, price_min_micro=500_000, price_max_micro=500_000)   # > PER_TX 0.2
    resp, _ = _pay(client, callee, payer_key, mandate, amount=500_000)
    assert resp.status_code == 403 and resp.get_json()["code"] == "MAX_AMOUNT_EXCEEDED"
    assert _spent(db, mandate) == 0


def test_category_outside_mandate_refused_and_nonce_released(client, db, callee, payer_key,
                                                             mandate, screener):
    callee.category = "Design"
    db.session.commit()
    resp, payload = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 403 and resp.get_json()["code"] == "CATEGORY_NOT_ALLOWED"
    nonce = payload["payload"]["authorization"]["nonce"]
    assert not client.application.extensions["agents_list.x402_nonces"].seen(nonce)
    assert _spent(db, mandate) == 0


def test_settlement_failure_gives_the_charge_back(client, db, app, callee, payer_key, mandate,
                                                  screener):
    class Broken:
        mode = "onchain"

        def settle_authorization(self, permit, *, pay_to, ref=None):
            raise RuntimeError("rpc down")
    app.extensions["agents_list.escrow"] = Broken()
    resp, _ = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 502 and resp.get_json()["code"] == "PAYMENT_FAILED"
    assert _spent(db, mandate) == 0
    assert [e.status for e in _ledger(mandate.engagement_id)] == ["failed"]


def test_payee_mismatch_refuses_before_402(client, db, app, callee):
    from app.models import EnsName

    class Resolver:
        def payout_address(self, name):
            return "0x" + "d" * 40
    db.session.add(EnsName(name="test-agent.agentslist-app.eth", kind="agent", agent_id=callee.id,
                           status="active", records={}, tx_hashes=[]))
    db.session.commit()
    app.extensions["ens_resolver"] = Resolver()
    resp = client.post(_url(callee), json={"task": "t"})
    assert resp.status_code == 403 and resp.get_json()["code"] == "PAYEE_MISMATCH"


class _PendingThenFailed:
    """On-chain escrow whose settlement is submitted (pending) and whose
    receipt later reports the transaction failed."""
    mode = "onchain"
    escrow_address = vault_address = None

    def __init__(self):
        self.receipt = "pending"

    def settle_authorization(self, permit, *, pay_to, ref=None):
        from chain.escrow import TxResult
        return TxResult(tx_hash="0x" + "ab" * 32, status="pending", explorer=None)

    def receipt_status(self, tx_hash):
        return self.receipt


def test_settlement_failed_on_chain_refunds_the_mandate_once(client, db, app, callee, payer_key,
                                                             mandate, screener):
    from app.api.tasks import settlement_failed
    from app.engagements import ledger
    from app.models import Engagement
    escrow = app.extensions["agents_list.escrow"] = _PendingThenFailed()
    resp, _ = _pay(client, callee, payer_key, mandate)
    assert resp.status_code == 200 and resp.get_json()["payment"]["status"] == "pending"
    assert _spent(db, mandate) == PRICE

    eid = mandate.engagement_id
    assert client.get(f"/api/engagements/{eid}").status_code == 200    # still pending
    assert _spent(db, mandate) == PRICE

    escrow.receipt = "failed"
    body = client.get(f"/api/engagements/{eid}").get_json()          # the poll path
    assert [e["status"] for e in body["ledger"]] == ["failed"]
    assert _spent(db, mandate) == 0
    # polling again (or a racing poller) never refunds twice
    client.get(f"/api/engagements/{eid}")
    (entry,) = _ledger(eid)
    assert ledger.refresh_receipts(db.session.get(Engagement, eid), escrow) == 0
    assert settlement_failed(entry) is False
    db.session.commit()
    assert _spent(db, mandate) == 0


def test_confirmed_settlement_keeps_the_charge(client, db, app, callee, payer_key, mandate,
                                               screener):
    escrow = app.extensions["agents_list.escrow"] = _PendingThenFailed()
    _pay(client, callee, payer_key, mandate)
    escrow.receipt = "confirmed"
    body = client.get(f"/api/engagements/{mandate.engagement_id}").get_json()
    assert [e["status"] for e in body["ledger"]] == ["confirmed"]
    assert _spent(db, mandate) == PRICE
