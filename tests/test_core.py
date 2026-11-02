import json

import httpx
from fastapi.testclient import TestClient

from tollgate.api import create_app
from tollgate.audit import AuditLog, replay
from tollgate.clients.heuristic import HeuristicClient
from tollgate.clients.jev import JevClient
from tollgate.compiler import compile_questions, load_policies
from tollgate.engine import TollgateGate, build_gate, demo_actions, pick_tier
from tollgate.types import Action, CheckResult, Thresholds, VendorContext

POLICIES = load_policies()


def payment(amount: float, doc_amount: float | None = None, **kw) -> Action:
    doc_amount = amount if doc_amount is None else doc_amount
    return Action(
        action_id=kw.pop("action_id", "t-pay"), tool="schedule_payment",
        args={"vendor_id": "V-ACME", "amount_usd": amount, "date": "2026-10-30", "memo": "INV-1"},
        operator_request="Pay invoice INV-1.", documents=[f"Invoice INV-1\nAmount due: ${doc_amount:,.2f}"],
        vendor_context=VendorContext(vendor_id="V-ACME", name="Acme", record_age_days=900), amount_usd=amount, **kw)


def test_compiler_question_shapes():
    qs = compile_questions(POLICIES.policies)
    assert qs["payee_bank_change"]["type"] == "noul"
    assert qs["intent"]["type"] == "choice" and isinstance(qs["intent"]["criteria"], dict)
    assert qs["overall_risk"]["type"] == "score" and len(qs["overall_risk"]["criteria"]) == 4
    assert len(POLICIES.file_hash) == 12


def test_tiers():
    assert pick_tier(POLICIES, payment(500))[0] == "payment_small"
    assert pick_tier(POLICIES, payment(5000))[0] == "payment_medium"
    assert pick_tier(POLICIES, payment(30000))[0] == "payment_large"
    assert pick_tier(POLICIES, demo_actions()[0])[0] == "bank_change"


def test_wednesday_gate_offline():
    gate = build_gate("heuristic")
    bank, pay = demo_actions()
    assert gate.decide(bank).verdict == "deny"
    assert gate.decide(pay).verdict == "allow"


def test_inflated_denied_and_large_escalated():
    gate = build_gate("heuristic")
    assert gate.decide(payment(42000, doc_amount=4200)).verdict == "deny"
    r = gate.decide(payment(38000))
    assert r.verdict == "escalate" and 0 < r.risk < 1


def test_threshold_override():
    gate = build_gate("heuristic")
    gate.override = Thresholds(allow_below=0.01, deny_above=0.99)
    assert gate.decide(payment(4200)).verdict == "escalate"


class CountingClient(HeuristicClient):
    calls = 0

    def evaluate(self, action, policies):
        CountingClient.calls += 1
        return super().evaluate(action, policies)


def test_double_check_on_large_payments():
    gate = TollgateGate(CountingClient(), POLICIES)
    gate.decide(payment(30000))
    assert CountingClient.calls == 2
    gate.decide(payment(3000))
    assert CountingClient.calls == 3


def fake_jev(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert request.url.path == "/v1/systemone" and body["model"] == "jev-1.14.0"
    assert body["state"]["operator_request"]
    answers = {}
    for name, q in body["questions"].items():
        if q["type"] == "noul":
            answers[name] = {"type": "noul", "noul": 0.94 if name == "payee_bank_change" else 0.05}
        elif q["type"] == "choice":
            answers[name] = {"type": "choice", "choice": "vendor_edit", "confidence": 0.9, "probabilities": {}}
        else:
            answers[name] = {"type": "score", "score": 2.6, "confidence": 0.7, "legend": {}, "probabilities": {}}
    return httpx.Response(200, json={"model": "jev-1.14.0", "answers": answers,
                                     "usage": {"input_tokens": 2000, "output_tokens": 0}})


def test_jev_client_parses_and_prices():
    client = JevClient("jev-1.14.0", usd_per_million_input_tokens=0.10, transport=httpx.MockTransport(fake_jev))
    gate = TollgateGate(client, POLICIES)
    r = gate.decide(demo_actions()[0])
    assert r.verdict == "deny"
    assert r.reasons[0].startswith("payee_bank_change p=0.94 > 0.60")
    assert abs(r.cost_usd - 2000 * 0.10 / 1e6) < 1e-12
    assert next(c for c in r.checks if c.policy_id == "overall_risk").score == 3


def test_jev_client_raises_on_error():
    client = JevClient("jev-1.14.0", 0.1, transport=httpx.MockTransport(
        lambda r: httpx.Response(422, json={"detail": [{"loc": ["body", "state"], "msg": "Field required"}]})))
    try:
        client.evaluate(demo_actions()[0], POLICIES.applicable(demo_actions()[0]))
    except RuntimeError as e:
        assert "422" in str(e)
    else:
        raise AssertionError("expected JevError")


def test_audit_and_replay():
    log, gate = AuditLog(), build_gate("heuristic")
    for a in demo_actions():
        log.record(a, gate.decide(a))
    assert [r.decision_id for r in log.list()] == [2, 1]
    assert log.list(since_id=1)[0].decision_id == 2
    strict = build_gate("heuristic")
    strict.override = Thresholds(allow_below=0.0, deny_above=0.0)
    changed = replay(log, strict)
    assert {c["decision_id"] for c in changed} == {2}


def test_api_routes():
    client = TestClient(create_app(build_gate("heuristic"), AuditLog()))
    assert client.get("/health").json()["ok"] is True
    for a in demo_actions() + [payment(38000, action_id="t-big")]:
        assert client.post("/check", json=a.model_dump(mode="json")).status_code == 200
    decisions = client.get("/decisions").json()
    assert [d["result"]["verdict"] for d in decisions] == ["escalate", "allow", "deny"]
    queue = client.get("/review").json()
    assert len(queue) == 1
    rid = queue[0]["decision_id"]
    assert client.post(f"/review/{rid}", json={"resolution": "approve"}).json()["resolution"] == "approve"
    assert client.get("/review").json() == []
    assert client.post("/review/1", json={"resolution": "approve"}).status_code == 409
    t = client.put("/thresholds", json={"allow_below": 0.2, "deny_above": 0.7}).json()
    assert client.get("/thresholds").json() == t
    assert client.put("/thresholds", json={"allow_below": 0.8, "deny_above": 0.2}).status_code == 422
