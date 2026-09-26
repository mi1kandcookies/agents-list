from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from app.approvals.service import create_approval
from app.identity.world import DeviceAuthorization
from app.models import Human
from tests.conftest import WALLET


class FakeWorldClient:
    def __init__(self):
        self.poll_count = 0

    def authorization_url(self, *, state, nonce, code_challenge):
        self.auth = {"state": state, "nonce": nonce, "challenge": code_challenge}
        return "https://world.test/authorize?" + f"state={state}"

    def exchange_code(self, code, code_verifier):
        assert code == "code-1"
        assert code_verifier
        return {"id_token": "auth-code-token"}

    def validate_id_token(self, token, *, expected_nonce=None):
        assert token in {"auth-code-token", "device-token"}
        if token == "auth-code-token":
            assert expected_nonce == self.auth["nonce"]
            return {"sub": "world-route", "nonce": expected_nonce,
                    "auth_time": int(datetime.now(timezone.utc).timestamp()), "jti": "route-jti-1"}
        assert expected_nonce is None
        return {"sub": "world-route", "auth_time": int(datetime.now(timezone.utc).timestamp()),
                "jti": "route-jti-2"}

    def start_device_flow(self, *, action_hash):
        self.device_action_hash = action_hash
        return DeviceAuthorization("device-1", "ABCD", "https://world.test/device", None, 600, 2)

    def poll_device(self, device_code):
        assert device_code == "device-1"
        self.poll_count += 1
        if self.poll_count == 1:
            return {"error": "authorization_pending", "interval": 2}
        return {"id_token": "device-token"}


def _approval(db, approval_id, human_id="human-world"):
    return create_approval(
        human_id=human_id, action_type="fund", terms={"actionType": "fund", "id": approval_id},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def test_world_auth_code_route_binds_action_hash_and_approves(client, app, db):
    with app.app_context():
        db.session.add(Human(id="human-world", world_sub="world-route", wallet=WALLET))
        db.session.commit()
        approval = _approval(db, "auth")
        approval_id = approval.id
        app.extensions["world_oidc_client"] = FakeWorldClient()

    started = client.get(f"/auth/world/start?approval_id={approval_id}")
    assert started.status_code == 302
    query = parse_qs(urlparse(started.headers["Location"]).query)
    callback = client.get(f"/auth/world/callback?state={query['state'][0]}&code=code-1")
    assert callback.status_code == 200, callback.get_json()
    assert callback.get_json()["state"] == "approved"

    status = client.get(f"/api/approvals/{approval_id}")
    assert status.get_json()["state"] == "approved"


def test_world_device_route_requires_bound_device_and_supports_pending_poll(client, app, db):
    with app.app_context():
        db.session.add(Human(id="human-world", world_sub="world-route", wallet=WALLET))
        db.session.commit()
        approval = _approval(db, "device")
        approval_id = approval.id
        app.extensions["world_oidc_client"] = FakeWorldClient()

    started = client.post("/auth/world/device", json={"approvalId": approval_id})
    assert started.status_code == 201, started.get_json()
    device = started.get_json()
    wrong = client.post("/auth/world/device/poll", json={"approvalId": approval_id, "deviceCode": "wrong"})
    assert wrong.status_code == 400

    pending = client.post("/auth/world/device/poll", json={"approvalId": approval_id,
                                                               "deviceCode": device["deviceCode"]})
    assert pending.status_code == 200 and pending.get_json()["state"] == "pending"
    approved = client.post("/auth/world/device/poll", json={"approvalId": approval_id,
                                                               "deviceCode": device["deviceCode"]})
    assert approved.status_code == 200 and approved.get_json()["state"] == "approved"
