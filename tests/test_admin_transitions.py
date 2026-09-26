import os
import unittest

os.environ["FLASK_ENV"] = "testing"
os.environ["AUTO_SEED_DATA"] = "1"
os.environ["ENABLE_SIM_ENGINE"] = "0"

from app import app  # noqa: E402
from extensions import db  # noqa: E402
from models import ModerationReport, Payout, VerificationEntry  # noqa: E402


class AdminTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.testing = True
        app.config["API_KEY"] = "test-admin-key"
        cls.client = app.test_client()
        cls.headers = {"X-Api-Key": "test-admin-key"}

    def tearDown(self):
        with app.app_context():
            VerificationEntry.query.filter(
                VerificationEntry.id.like("TST-VRF-%")
            ).delete(synchronize_session=False)
            Payout.query.filter(Payout.id.like("TST-PAY-%")).delete(synchronize_session=False)
            ModerationReport.query.filter(
                ModerationReport.id.like("TST-RPT-%")
            ).delete(synchronize_session=False)
            db.session.commit()

    def test_admin_requires_api_key(self):
        resp = self.client.post("/admin/payouts/release-all")
        self.assertEqual(resp.status_code, 401)

    def test_release_rejected_when_refunded(self):
        with app.app_context():
            db.session.add(
                Payout(
                    id="TST-PAY-001",
                    seller="seller",
                    agent="agent",
                    amount=10.0,
                    status="refunded",
                    date="2026-04-20",
                )
            )
            db.session.commit()

        resp = self.client.post("/admin/payouts/TST-PAY-001/release", headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body.get("code"), "INVALID_STATE")

    def test_refund_rejected_when_released(self):
        with app.app_context():
            db.session.add(
                Payout(
                    id="TST-PAY-002",
                    seller="seller",
                    agent="agent",
                    amount=20.0,
                    status="released",
                    date="2026-04-20",
                )
            )
            db.session.commit()

        resp = self.client.post("/admin/payouts/TST-PAY-002/refund", headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body.get("code"), "INVALID_STATE")

    def test_reject_verification_not_found_shape(self):
        resp = self.client.post("/admin/verification-queue/TST-VRF-MISSING/reject", headers=self.headers)
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(body.get("code"), "VERIFICATION_NOT_FOUND")

    def test_resolve_report_not_found_shape(self):
        resp = self.client.post("/admin/moderation/TST-RPT-MISSING/resolve", headers=self.headers)
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(body.get("code"), "REPORT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
