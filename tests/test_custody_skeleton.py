"""Custody-chain skeleton: migration round trip, models, boot, test fakes."""
import sqlalchemy as sa
from flask_migrate import downgrade, upgrade

from app import MIGRATIONS_DIR, create_app
from app.common.agent_ids import from_db_id, is_valid
from app.extensions import db

CUSTODY_TABLES = {"humans", "engagements", "milestones", "ledger_entries", "approvals",
                  "approval_events", "mandates", "screenings", "ens_names", "used_id_token_jtis"}


def test_migration_upgrade_downgrade_upgrade_and_backfill(tmp_path):
    app = create_app("testing", SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'm.db'}")
    with app.app_context():
        upgrade(directory=MIGRATIONS_DIR, revision="0001_initial")
        with db.engine.begin() as conn:
            for i in (1, 2, 7):
                conn.execute(sa.text(
                    "INSERT INTO agents (id, name, description, long_description, category, use_case,"
                    " verified, verification_tier, featured, rating, reviews, billing, min_price,"
                    " max_price, current_price, seller, seller_rating, tasks_completed,"
                    " avg_completion_time, input_price_per_1m, output_price_per_1m, tags, capabilities)"
                    " VALUES (:i, 'a', '', '', 'Dev', '', 0, 'none', 0, 0, 0, 'per_minute', 0, 0, 0,"
                    " '', 0, 0, '-', 0, 0, '[]', '[]')"), {"i": i})
        upgrade(directory=MIGRATIONS_DIR)
        tables = set(sa.inspect(db.engine).get_table_names())
        assert CUSTODY_TABLES <= tables
        with db.engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT id, public_id FROM agents ORDER BY id")).all()
        assert rows == [(i, from_db_id(i)) for i in (1, 2, 7)]

        downgrade(directory=MIGRATIONS_DIR, revision="0001_initial")
        inspector = sa.inspect(db.engine)
        assert not CUSTODY_TABLES & set(inspector.get_table_names())
        assert "public_id" not in {c["name"] for c in inspector.get_columns("agents")}

        upgrade(directory=MIGRATIONS_DIR)
        assert CUSTODY_TABLES <= set(sa.inspect(db.engine).get_table_names())


def test_new_agents_get_a_public_id(db, agent):
    from app.models import Agent
    row = db.session.get(Agent, agent)
    assert row.public_id == from_db_id(agent) and is_valid(row.public_id)


def test_custody_rows_get_prefixed_ids(db, agent, human):
    from datetime import datetime, timedelta, timezone

    from app.models import Approval, ApprovalEvent, Engagement, LedgerEntry, Milestone
    eng = Engagement(agent_id=agent, buyer_human_id=human.id, outcome="x", total_micro=25_000_000)
    eng.milestones.append(Milestone(idx=0, title="t", amount_micro=25_000_000))
    db.session.add(eng)
    db.session.flush()
    apr = Approval(kind="engagement.fund", action_json="{}", action_hash="0x", nonce="n",
                   flow="device", expires_at=datetime.now(timezone.utc) + timedelta(minutes=3),
                   engagement_id=eng.id)
    apr.events.append(ApprovalEvent(event="created", detail={"k": 1}))
    db.session.add_all([apr, LedgerEntry(engagement_id=eng.id, kind="fund", amount_micro=1)])
    db.session.commit()
    assert eng.id.startswith("ENG-") and apr.id.startswith("APR-")
    assert db.session.query(LedgerEntry).one().id.startswith("LED-")
    assert apr.events[0].detail == {"k": 1}


def test_app_boots_with_no_env_vars(monkeypatch):
    for name in ("WORLD_ISSUER", "WORLD_CLIENT_ID", "WORLD_CLIENT_SECRET", "WORLD_REDIRECT_URI",
                 "WORLD_REQUIRED_ACR", "APPROVAL_TTL_SECONDS", "STEPUP_MAX_AGE_SECONDS",
                 "HUMAN_WEEKLY_CAP_USDC", "INTERCEPTA_API_KEY", "INTERCEPTA_BASE_URL",
                 "SCREENING_TIMEOUT_SECONDS", "SCREENING_CAP_USDC", "SCREENING_ADDRESS_MAP",
                 "ESCROW_PRIVATE_KEY", "BUYER_VAULT_PRIVATE_KEY", "MANDATE_SIGNING_KEY",
                 "MANDATE_MAX_DEPTH", "ENS_SIDECAR_URL", "ENS_SIDECAR_TOKEN", "ENS_ROOT_NAME",
                 "ENS_OPERATOR_PRIVATE_KEY", "MCP_API_BASE", "MCP_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    app = create_app("testing")
    for name in ("identity", "approvals", "engagements", "screening", "mandates", "humans", "names"):
        assert name in app.blueprints
    with app.app_context():
        db.create_all()
        assert app.test_client().get("/api/health").status_code == 200


def test_fake_world_idp_device_flow(world_idp):
    import jwt
    import requests

    from app.identity.world import WorldClient
    client = WorldClient()  # configured from the env the fixture sets
    start = client.device_authorize(nonce="n" * 43)
    assert client.poll_device(start.device_code).status == "pending"
    world_idp.approve_device(start.device_code, acr="orb")
    poll = client.poll_device(start.device_code)
    assert poll.status == "approved"
    claims = jwt.decode(poll.id_token, options={"verify_signature": False})
    assert claims["nonce"] == "n" * 43 and claims["acr"] == "orb"
    jwks = requests.get(world_idp.issuer + "/jwks.json").json()
    assert jwks["keys"][0]["kid"] == jwt.get_unverified_header(poll.id_token)["kid"]

    denied = client.device_authorize()
    world_idp.deny_device(denied.device_code)
    assert client.poll_device(denied.device_code).status == "denied"
    expired = client.device_authorize()
    world_idp.expire_device(expired.device_code)
    assert client.poll_device(expired.device_code).status == "expired"


def test_fake_screener_and_escrow(fake_screener, fake_escrow):
    addr = "0x" + "ab" * 20
    fake_screener.set(addr, "REFUSE", fail_closed=True)
    v = fake_screener.screen("milestone.release", chain_address=addr.upper().replace("0X", "0x"),
                             amount_micro=1)
    assert v["verdict"] == "REFUSE" and v["fail_closed"] and v["id"].startswith("SCR-")
    assert fake_screener.screen("payer.check", chain_address="0x" + "cd" * 20,
                                amount_micro=1)["verdict"] == "PAY"
    tx = fake_escrow.release(to=addr, amount_micro=5)
    assert tx.status == "simulated" and fake_escrow.calls[0][0] == "release"
    assert fake_escrow.receipt_status(tx.tx_hash) == "confirmed"
