"""Dashboard routes round-trip against the stub API and the real API (offline gate)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import world
from agent.demo_scenario import MAX_STEPS, REQUEST
from agent.run import run_task
from tollgate import stub_api
from tollgate.api import create_app
from tollgate.audit import AuditLog
from tollgate.engine import build_gate

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard" / "index.html"


class ClientGate:
    """Sends actions through POST /check on a TestClient, like HttpGate does over the network."""
    name = "testclient"

    def __init__(self, client):
        self.client = client

    def decide(self, action):
        from tollgate.types import DecisionResult
        r = self.client.post("/check", json=action.model_dump(mode="json"))
        r.raise_for_status()
        return DecisionResult.model_validate(r.json())


@pytest.fixture(params=["stub", "real"])
def client(request):
    if request.param == "stub":
        return TestClient(stub_api.create_app(seeded=False))
    return TestClient(create_app(gate=build_gate("heuristic"), log=AuditLog()))


def test_demo_through_api_then_review_and_thresholds(client):
    world.reset()
    run_task(REQUEST, ClientGate(client), max_steps=MAX_STEPS)
    world.reset()
    recs = client.get("/decisions", params={"limit": 100}).json()
    assert len(recs) == 31
    ids = [r["decision_id"] for r in recs]
    assert ids == sorted(ids, reverse=True)                     # newest first
    assert len(client.get("/decisions", params={"limit": 5}).json()) == 5
    assert [r["decision_id"] for r in client.get("/decisions", params={"since_id": ids[2]}).json()] == ids[:2]
    verdicts = {r["action"]["tool"] + ":" + str(r["action"]["args"].get("memo", "")): r["result"]["verdict"] for r in recs}
    assert verdicts["schedule_payment:INV-1042"] == "allow"
    assert verdicts["update_vendor:"] == "deny"
    assert verdicts["schedule_payment:INV-8090"] == "escalate"

    queue = client.get("/review").json()
    assert [r["action"]["args"]["memo"] for r in queue] == ["INV-8090"]
    did = queue[0]["decision_id"]
    out = client.post(f"/review/{did}", json={"resolution": "approve"})
    assert out.status_code == 200 and out.json()["resolution"] == "approve"
    assert client.get("/review").json() == []
    assert client.post(f"/review/{ids[0]}", json={"resolution": "reject"}).status_code == 409   # not escalated
    assert client.post("/review/99999", json={"resolution": "reject"}).status_code == 404
    assert client.post(f"/review/{did}", json={"resolution": "maybe"}).status_code == 422

    t = client.get("/thresholds").json()
    assert set(t) == {"allow_below", "deny_above"}
    assert client.put("/thresholds", json={"allow_below": 0.2, "deny_above": 0.7}).json() == {"allow_below": 0.2, "deny_above": 0.7}
    assert client.get("/thresholds").json() == {"allow_below": 0.2, "deny_above": 0.7}
    assert client.put("/thresholds", json={"allow_below": 0.9, "deny_above": 0.1}).status_code == 422
    assert client.get("/health").json()["ok"] is True


def test_reject_on_stub_seeded_data():
    c = TestClient(stub_api.create_app())
    recs = c.get("/decisions").json()
    assert len(recs) == 31 and {r["result"]["verdict"] for r in recs} == {"allow", "escalate", "deny"}
    did = c.get("/review").json()[0]["decision_id"]
    assert c.post(f"/review/{did}", json={"resolution": "reject"}).json()["resolution"] == "reject"
    assert c.get("/review").json() == []


def test_dashboard_page_served_and_self_contained():
    html = DASHBOARD.read_text()
    for route in ("/decisions", "/review", "/thresholds"):
        assert route in html
    assert "<script src" not in html                          # no external JS
    r = TestClient(stub_api.create_app(seeded=False)).get("/")
    assert r.status_code == 200 and "Tollgate" in r.text
