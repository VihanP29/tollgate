"""FastAPI app: /check plus the routes the dashboard uses (SPEC section 6)."""
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tollgate.audit import AuditLog
from tollgate.engine import TollgateGate, build_gate
from tollgate.types import Action, DecisionRecord, DecisionResult, Thresholds

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


class Resolution(BaseModel):
    resolution: Literal["approve", "reject"]


def create_app(gate: TollgateGate | None = None, log: AuditLog | None = None) -> FastAPI:
    gate = gate or build_gate(os.environ.get("TOLLGATE_BACKEND", "auto"))
    log = log or AuditLog(os.environ.get("TOLLGATE_DB", ":memory:"))
    app = FastAPI(title="Tollgate")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.gate, app.state.log = gate, log

    @app.post("/check", response_model=DecisionResult)
    def check(action: Action) -> DecisionResult:
        result = gate.decide(action)
        log.record(action, result)
        return result

    @app.get("/decisions", response_model=list[DecisionRecord])
    def decisions(limit: int = 50, since_id: int | None = None):
        return log.list(limit=limit, since_id=since_id)

    @app.get("/review", response_model=list[DecisionRecord])
    def review():
        return log.pending_review()

    @app.post("/review/{decision_id}", response_model=DecisionRecord)
    def resolve(decision_id: int, body: Resolution):
        rec = log.get(decision_id)
        if rec is None:
            raise HTTPException(404, "no such decision")
        if rec.result.verdict != "escalate":
            raise HTTPException(409, "only escalated decisions can be resolved")
        return log.resolve(decision_id, body.resolution)

    @app.get("/thresholds", response_model=Thresholds)
    def get_thresholds():
        if gate.override is not None:
            return gate.override
        t = gate.policies.tiers["default"]
        return Thresholds(allow_below=t.allow_below, deny_above=t.deny_above)

    @app.put("/thresholds", response_model=Thresholds)
    def put_thresholds(t: Thresholds):
        if not 0 <= t.allow_below <= t.deny_above <= 1:
            raise HTTPException(422, "need 0 <= allow_below <= deny_above <= 1")
        gate.override = t
        return t

    @app.get("/health")
    def health():
        return {"ok": True, "gate": gate.name, "model": gate.client.model}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(DASHBOARD)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("tollgate.api:app", host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
