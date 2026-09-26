"""Every registered parameterless GET route must respond without a server error,
on an empty database and on one with sample listings."""
import pytest

from tests.conftest import WALLET


def _get_routes(app):
    rules = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint == "static" or rule.arguments:
            continue
        rules.append(rule.rule)
    return sorted(set(rules))


def test_route_inventory_is_not_empty(app):
    assert len(_get_routes(app)) >= 20


@pytest.mark.parametrize("seed", [False, True], ids=["empty-db", "sample-db"])
def test_parameterless_get_routes_do_not_500(app, client, db, seed):
    if seed:
        from app.models import Agent
        from app.sample_data import seed_sample_agents
        seed_sample_agents(db, Agent)
    client.set_cookie("buyer_wallet", WALLET)
    failures = {}
    for url in _get_routes(app):
        resp = client.get(url)
        if resp.status_code >= 500:
            failures[url] = resp.status_code
    assert not failures, failures


KEPT_PAGES = [
    "/", "/marketplace", "/how-it-works", "/active-jobs", "/past-jobs",
    "/seller/create", "/seller/earnings", "/seller/orders", "/seller/verification",
    "/admin/dashboard", "/admin/verification-queue", "/admin/moderation", "/admin/payouts",
]


@pytest.mark.parametrize("url", KEPT_PAGES)
def test_kept_pages_return_200(client, url):
    assert client.get(url).status_code == 200


def test_parameterized_pages_return_200(client, agent):
    for url in (f"/agent/{agent}", f"/checkout/{agent}", f"/seller/agents/{agent}"):
        assert client.get(url).status_code == 200, url
    resp = client.post(f"/checkout/{agent}", json={"task": "x", "amount": 5, "buyer": WALLET})
    assert resp.status_code == 201
    assert client.get(f"/order/{resp.get_json()['orderId']}").status_code == 200


@pytest.mark.parametrize("url", ["/demo", "/sim", "/agent-mode", "/admin/sandbox",
                                 "/api/sim/status", "/api/auctions", "/api/icm/info",
                                 "/api/stack", "/api/price/1"])
def test_dropped_routes_are_gone(client, url):
    assert client.get(url).status_code == 404


def test_health_and_ready(client):
    assert client.get("/api/health").get_json()["service"] == "agents-list"
    assert client.get("/api/ready").get_json()["db"] == "ok"
