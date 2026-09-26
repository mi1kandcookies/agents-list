"""Shared pytest fixtures: a fresh app + in-memory database per test."""
from __future__ import annotations

import os

import pytest

# Keep tests hermetic: no network chain calls, no keys, no LLM.
for _var in ("FACILITATOR_PRIVATE_KEY", "GATEKEEPER_PRIVATE_KEY", "PRIVATE_KEY",
             "LLM_URL", "API_KEY", "DATABASE_URL"):
    os.environ.pop(_var, None)
os.environ["FLASK_ENV"] = "testing"

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402

WALLET = "0x" + "b" * 40


@pytest.fixture()
def app():
    app = create_app("testing")
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def agent(db):
    """One listed agent, returned as its id."""
    from app.models import Agent
    row = Agent(name="Test Agent", description="Does tests", category="Development",
                billing="per_minute", min_price=0.05, max_price=0.2, current_price=0.1,
                seller=WALLET, deployer_wallet=WALLET)
    row.tags = ["testing"]
    row.capabilities = ["Writes tests"]
    db.session.add(row)
    db.session.commit()
    return row.id
