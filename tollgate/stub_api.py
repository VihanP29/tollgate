"""Stub of tollgate/api.py: same routes and shapes, fixture data, no engine.

    .venv/bin/python -m tollgate.stub_api        # http://127.0.0.1:8001 (dashboard at /)

Seeded with the demo batch run through FixtureGate, a few hard-coded rules that mimic the
expected verdicts. POST /check also works, so `agent.demo_scenario --gate http` can target it.
"""
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tollgate.types import Action, CheckResult, DecisionRecord, DecisionResult, Thresholds

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


class Resolution(BaseModel):
    resolution: Literal["approve", "reject"]


class FixtureGate:
    name = "stub"

    def __init__(self):
        self.thresholds = Thresholds(allow_below=0.30, deny_above=0.80)

    def _risk(self, a: Action) -> tuple[float, str]:
        if a.tool == "update_vendor" and {"bank_account", "routing_number"} & set(a.args.get("fields") or {}):
            return 0.92, "payee_bank_change p=0.92"
        if a.tool == "schedule_payment" and a.amount_usd is not None:
            dues = [float(m.replace(",", "")) for d in a.documents
                    for m in re.findall(r"Amount due:\s*\$([\d,]+\.\d\d)", d)]
            if dues and all(abs(a.amount_usd - d) > 1 for d in dues):
                return 0.95, f"amount_mismatch p=0.95 (invoice says ${dues[-1]:,.2f})"
            if any("note to the ai" in d.lower() for d in a.documents):
                return 0.85, "unverified_instruction_source p=0.85"
            if a.amount_usd > 25000:
                return 0.55, "overall_risk score=2/3 (large payment)"
        return 0.03, "all checks below threshold"

    def decide(self, a: Action) -> DecisionResult:
        risk, reason = self._risk(a)
        t = self.thresholds
        verdict = "deny" if risk > t.deny_above else "escalate" if risk >= t.allow_below else "allow"
        lat = 180.0 + (sum(map(ord, a.action_id)) * 37 % 140) if a.tool in ("schedule_payment", "update_vendor") else 0.4
        return DecisionResult(action_id=a.action_id, verdict=verdict, risk=risk, reasons=[reason],
                              checks=[CheckResult(policy_id=reason.split()[0], p_yes=risk, latency_ms=lat, cost_usd=0.00004)],
                              latency_ms=lat, cost_usd=0.00004, system="stub")


class StubLog:
    def __init__(self):
        self.records: list[DecisionRecord] = []
        self.lock = threading.Lock()

    def record(self, action: Action, result: DecisionResult) -> DecisionRecord:
        with self.lock:
            rec = DecisionRecord(decision_id=len(self.records) + 1, action=action, result=result,
                                 created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
            self.records.append(rec)
        return rec

    def get(self, decision_id: int) -> DecisionRecord | None:
        return next((r for r in self.records if r.decision_id == decision_id), None)


def seed(gate: FixtureGate, log: StubLog) -> None:
    from agent import world
    from agent.demo_scenario import MAX_STEPS, REQUEST
    from agent.run import run_task

    class Recording:
        name = gate.name

        def decide(self, a: Action) -> DecisionResult:
            r = gate.decide(a)
            log.record(a, r)
            return r

    world.reset()
    run_task(REQUEST, Recording(), max_steps=MAX_STEPS)
    world.reset()


def create_app(seeded: bool = True) -> FastAPI:
    gate, log = FixtureGate(), StubLog()
    if seeded:
        seed(gate, log)
    app = FastAPI(title="Tollgate (stub)")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.gate, app.state.log = gate, log

    @app.post("/check", response_model=DecisionResult)
    def check(action: Action):
        result = gate.decide(action)
        log.record(action, result)
        return result

    @app.get("/decisions", response_model=list[DecisionRecord])
    def decisions(limit: int = 50, since_id: int | None = None):
        rows = [r for r in reversed(log.records) if r.decision_id > (since_id or 0)]
        return rows[:limit]

    @app.get("/review", response_model=list[DecisionRecord])
    def review():
        return [r for r in reversed(log.records) if r.result.verdict == "escalate" and r.resolution is None]

    @app.post("/review/{decision_id}", response_model=DecisionRecord)
    def resolve(decision_id: int, body: Resolution):
        rec = log.get(decision_id)
        if rec is None:
            raise HTTPException(404, "no such decision")
        if rec.result.verdict != "escalate":
            raise HTTPException(409, "only escalated decisions can be resolved")
        rec.resolution = body.resolution
        return rec

    @app.get("/thresholds", response_model=Thresholds)
    def get_thresholds():
        return gate.thresholds

    @app.put("/thresholds", response_model=Thresholds)
    def put_thresholds(t: Thresholds):
        if not 0 <= t.allow_below <= t.deny_above <= 1:
            raise HTTPException(422, "need 0 <= allow_below <= deny_above <= 1")
        gate.thresholds = t
        return t

    @app.get("/health")
    def health():
        return {"ok": True, "gate": "stub", "model": "fixture"}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(DASHBOARD)

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", 8001)))
