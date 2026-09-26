import os
import unittest


# Ensure test-safe runtime mode before importing app module.
os.environ["FLASK_ENV"] = "testing"
os.environ["AUTO_SEED_DATA"] = "1"
os.environ["ENABLE_SIM_ENGINE"] = "0"

from app import app  # noqa: E402


class ApiValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.testing = True
        cls.client = app.test_client()

    def test_x402_pay_rejects_invalid_wallet(self):
        payload = {
            "from": "0x123",
            "to": "0x456",
            "value": 0,
            "validBefore": 1,
            "nonce": 0,
            "v": 27,
            "r": "0x0",
            "s": "0x0",
            "agentId": 9999,
        }
        resp = self.client.post("/api/x402/pay", json=payload)
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body.get("code"), "INVALID_REQUEST")
        self.assertEqual(body.get("field"), "from")

    def test_sim_speed_rejects_out_of_range_tick(self):
        resp = self.client.post("/api/sim/speed", json={"tickRealSeconds": 0.01})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body.get("field"), "tickRealSeconds")

    def test_sim_live_mode_requires_enabled_field(self):
        resp = self.client.post("/api/sim/live-mode", json={})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body.get("field"), "enabled")

    def test_agent_register_requires_valid_wallet(self):
        payload = {"wallet": "0x123", "name": "ab", "endpointURL": "foo"}
        resp = self.client.post("/api/agents/register", json=payload)
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body.get("field"), "wallet")


if __name__ == "__main__":
    unittest.main()
