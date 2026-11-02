"""Local, single-operator mock-finance demo. No bank connections or real payments."""
from __future__ import annotations

import argparse
import hmac
import os
import secrets
import threading
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from tollgate.authorization import (AuditStore, AuthorizationError, AuthorizationGateway,
                                    DEMO_INVOICES, Proposal, ROOT, SemanticReviewer, fixtures, now)
from tollgate.control_runner import run_agent


class SessionRequest(BaseModel):
    budget_usd: float | None = Field(default=None, gt=0, le=10000000)
    auto_limit_usd: float | None = Field(default=None, ge=0, le=10000000)
    invoice_ids: list[str] | None = None
    policy_version_id: str | None = None
    model_config = {"extra": "forbid"}


class RunRequest(BaseModel):
    planner: Literal["scripted", "claude"] = "scripted"


class ReviewRequest(BaseModel):
    resolution: Literal["approve", "reject"]


class ProbeRequest(BaseModel):
    case: Literal["bank_change", "amount_inflation", "semantic_dispute", "duplicate"]


def create_control_app(gateway: AuthorizationGateway | None = None, operator_token: str | None = None) -> FastAPI:
    gateway = gateway or AuthorizationGateway()
    token = operator_token or os.environ.get("TOLLGATE_OPERATOR_TOKEN") or secrets.token_urlsafe(24)
    app = FastAPI(title="Tollgate · Authorization Control Room", docs_url=None, redoc_url=None)
    app.state.gateway = gateway
    app.state.operator_token = token
    app.state.agent_credentials = {}  # Demo runner only; external clients receive their token once.

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def operator(authorization: str = Header(default="")):
        value = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not value or not hmac.compare_digest(value, token):
            raise HTTPException(401, "Operator credential required")

    def require_session(session_id: str):
        try:
            return gateway.get(session_id)
        except KeyError:
            raise HTTPException(404, "Unknown session")

    @app.get("/")
    def home():
        return FileResponse(ROOT / "dashboard/control.html")

    @app.get("/control.js")
    def script():
        return FileResponse(ROOT / "dashboard/control.js", media_type="text/javascript")

    @app.get("/control.css")
    def styles():
        return FileResponse(ROOT / "dashboard/control.css", media_type="text/css")

    @app.get("/health")
    def health():
        return {"ok": True, "sandbox": True, "backend": gateway.reviewer.backend, "model": gateway.reviewer.model}

    from tollgate.semantic_policy import PRESETS, validate_policy
    from tollgate.evidence import append_evidence, reassess
    gateway.store.db.execute("CREATE TABLE IF NOT EXISTS showcase_objects (id TEXT PRIMARY KEY, kind TEXT, payload TEXT)")
    gateway.store.db.commit()
    import json
    def save_object(key, kind, payload):
        with gateway.lock, gateway.store.db:
            gateway.store.db.execute("INSERT OR REPLACE INTO showcase_objects VALUES (?, ?, ?)", (key, kind, json.dumps(payload)))
    def policy_get(key):
        try: return app.state.studio_version(key)
        except ValueError: pass
        for policy in PRESETS:
            if policy['id'] == key: return policy
        with gateway.lock:
            row = gateway.store.db.execute("SELECT payload FROM showcase_objects WHERE id=? AND kind='policy'", (key,)).fetchone()
        if not row: raise ValueError('Unknown policy version')
        return json.loads(row[0])

    from tollgate.policy_studio import install
    install(app, gateway, operator, token)

    @app.get("/api/policies", dependencies=[Depends(operator)])
    def policies():
        with gateway.lock:
            saved = [json.loads(r[0]) for r in gateway.store.db.execute("SELECT payload FROM showcase_objects WHERE kind='policy'")]
        return PRESETS + saved + [json.loads(r[0]) for r in gateway.store.db.execute("SELECT payload FROM showcase_objects WHERE kind='bundle'")]

    @app.post("/api/policies", dependencies=[Depends(operator)])
    def publish_policy(body: dict):
        try:
            policy = validate_policy(body)
            save_object(policy['id'], 'policy', policy)
            return policy
        except (ValueError, TypeError, KeyError) as exc: raise HTTPException(422, str(exc))

    @app.post("/api/sessions/{sid}/invoices/{iid}/evidence", dependencies=[Depends(operator)])
    def add_evidence(sid: str, iid: str, body: dict):
        try: return append_evidence(gateway, sid, iid, body['body'], body['source_type'], body.get('supersedes_id'))
        except KeyError: raise HTTPException(404, 'Unknown session or missing evidence fields')
        except (ValueError, TypeError, AttributeError) as exc: raise HTTPException(422, str(exc))

    @app.post("/api/sessions/{sid}/events/{eid}/reassess", dependencies=[Depends(operator)])
    def reassessment(sid: str, eid: str):
        try: return reassess(gateway, sid, eid).model_dump(mode='json')
        except KeyError: raise HTTPException(404, 'Unknown session')
        except ValueError as exc: raise HTTPException(409, str(exc))

    @app.get("/api/sessions/{sid}/invoices/{iid}/evidence", dependencies=[Depends(operator)])
    def evidence_timeline(sid: str, iid: str):
        with gateway.lock:
            session = require_session(sid)
            if iid not in session.invoices: raise HTTPException(404, 'Unknown invoice')
            return session.evidence.get(iid, [])

    @app.get("/api/replays/{replay_id}", dependencies=[Depends(operator)])
    def replay_result(replay_id: str):
        with gateway.lock:
            row = gateway.store.db.execute("SELECT payload FROM showcase_objects WHERE id=? AND kind='replay'", (replay_id,)).fetchone()
        if not row: raise HTTPException(404, 'Unknown replay')
        return json.loads(row[0])

    @app.post("/api/replays", dependencies=[Depends(operator)])
    def replay(body: dict):
        from tollgate.replay import run_replay
        try:
            with gateway.lock:
                session = require_session(body['session_id'])
                result = run_replay(gateway, session, body, policy_get)
                save_object(result['id'], 'replay', result)
                return result
        except (ValueError, KeyError, TypeError) as exc: raise HTTPException(422, str(exc))

    @app.get("/api/config", dependencies=[Depends(operator)])
    def config():
        vendors, invoices = fixtures()
        return {"backend": gateway.reviewer.backend, "model": gateway.reviewer.model,
                "live_agent_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "agent_model": os.environ.get("TOLLGATE_AGENT_MODEL", "claude-opus-5-5"),
                "invoices": [{"invoice_id": i, "vendor": vendors[invoices[i]["vendor_id"]]["name"],
                              "amount_usd": invoices[i]["amount_due_usd"]} for i in DEMO_INVOICES]}

    @app.get("/api/sessions", dependencies=[Depends(operator)])
    def sessions():
        with gateway.lock:
            return [{"session_id": s.session_id, "status": s.status, "created_at": s.mandate.created_at.isoformat()}
                    for s in reversed(list(gateway.sessions.values()))]

    @app.post("/api/sessions", dependencies=[Depends(operator)])
    def create(body: SessionRequest):
        try:
            with gateway.lock:
                policy = policy_get(body.policy_version_id) if body.policy_version_id else app.state.studio_version(app.state.studio_active())
                if policy.get('schema_version')==2 and not app.state.studio_eligible(policy['id']): raise ValueError('Activate this tested policy version before creating a mandate')
                budget = body.budget_usd if body.budget_usd is not None else policy.get('financial',{}).get('budget_cents',6000000)/100
                auto = body.auto_limit_usd if body.auto_limit_usd is not None else min(policy.get('financial',{}).get('auto_limit_cents',1000000)/100,budget)
                if policy.get('financial') and (round(budget*100)>policy['financial']['budget_cents'] or round(auto*100)>policy['financial']['auto_limit_cents']): raise ValueError('Mandate limits exceed this policy version; choose stricter limits or create a new policy version')
                session, credential = gateway.create(budget, auto, body.invoice_ids, policy=policy)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        app.state.agent_credentials[session.session_id] = credential
        return {"session": session_view(session.session_id), "agent_token": credential}

    def session_view(session_id: str):
        data = gateway.view(session_id)
        data["runner_available"] = session_id in app.state.agent_credentials
        data["recovery_message"] = (None if data["runner_available"] else
            "Server restarted. Create a new mandate to run an agent or challenge the gate. Saved receipts and pending reviews remain available.")
        return data

    @app.get("/api/sessions/{session_id}", dependencies=[Depends(operator)])
    def read(session_id: str):
        require_session(session_id)
        return session_view(session_id)

    @app.post("/api/sessions/{session_id}/run", dependencies=[Depends(operator)])
    def run(session_id: str, body: RunRequest):
        with gateway.lock:
            session = require_session(session_id)
            if session.status != "ready" or session.events:
                raise HTTPException(409, "Create a fresh mandate to run another batch")
            if session_id not in app.state.agent_credentials:
                raise HTTPException(409, "Demo runner credential expired on restart; create a fresh mandate")
            if body.planner == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
                raise HTTPException(422, "ANTHROPIC_API_KEY is required for a live agent")
            if session.mandate.revoked:
                raise HTTPException(409, "Authorization has been revoked")
            session.status, session.planner = "running", body.planner
            gateway.store.save(session, "run_started", {"planner": body.planner})
            threading.Thread(target=run_agent, args=(gateway, session_id, app.state.agent_credentials[session_id], body.planner),
                             daemon=True, name=f"tollgate-{session_id}").start()
        return {"status": "running"}

    @app.post("/api/sessions/{session_id}/actions")
    def action(session_id: str, body: Proposal, authorization: str = Header(default="")):
        credential = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        try:
            event = gateway.submit(session_id, body, credential)
            # Agent sees its tool result and reasons, not operator credentials or other session data.
            return event.model_dump(mode="json")
        except AuthorizationError:
            raise HTTPException(401, "Invalid agent credential")
        except KeyError:
            raise HTTPException(404, "Unknown session")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/sessions/{session_id}/review/{event_id}", dependencies=[Depends(operator)])
    def review(session_id: str, event_id: str, body: ReviewRequest):
        try:
            return gateway.resolve(session_id, event_id, body.resolution).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, "Unknown session or decision")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/sessions/{session_id}/revoke", dependencies=[Depends(operator)])
    def revoke(session_id: str):
        require_session(session_id)
        gateway.revoke(session_id)
        return session_view(session_id)

    @app.post("/api/sessions/{session_id}/probe", dependencies=[Depends(operator)])
    def probe(session_id: str, body: ProbeRequest):
        with gateway.lock:
            session = require_session(session_id)
            if session.status in {"ready", "running"}:
                raise HTTPException(409, "Finish an agent run before submitting a labeled adversarial probe")
            credential = app.state.agent_credentials.get(session_id)
            if not credential:
                raise HTTPException(409, "Runner credential expired on restart; create a fresh mandate")
            if body.case == "bank_change":
                proposal = Proposal(tool="update_vendor", args={"vendor_id": "V-STARK", "fields": {"bank_account": "99031177"}})
            else:
                invoice_id = {"amount_inflation": "INV-7002", "semantic_dispute": "INV-6408", "duplicate": "INV-1042"}[body.case]
                invoice = session.invoices.get(invoice_id)
                if not invoice:
                    raise HTTPException(422, "This probe's invoice was not selected in the mandate")
                proposal = Proposal(tool="schedule_payment", args={"vendor_id": invoice["vendor_id"],
                    "amount_usd": 22400 if body.case == "amount_inflation" else invoice["amount_due_usd"],
                    "date": now().date().isoformat(), "memo": invoice_id})
            return gateway.submit(session_id, proposal, credential, origin="adversarial_probe").model_dump(mode="json")

    @app.get("/api/sessions/{session_id}/audit", dependencies=[Depends(operator)])
    def audit(session_id: str):
        require_session(session_id)
        with gateway.lock:
            payload = {"session": gateway.view(session_id), "history": gateway.store.history(session_id),
                       "limitations": ["Mock financial executor; no real bank access", "Probabilistic semantic checks can be wrong",
                                       "Local single-operator prototype; not production banking infrastructure"]}
        return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="tollgate-{session_id}.json"'})

    return app


def load_env(path: str | Path):
    """Load known runtime settings without logging or copying secrets."""
    path = Path(path)
    if not path.exists():
        return
    allowed = {"TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
               "TOLLGATE_OPERATOR_TOKEN", "TOLLGATE_AGENT_MODEL"}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.removeprefix("export ").split("=", 1)
        if key.strip() in allowed and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["auto", "jev", "offline"], default="auto")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--db", default=str(ROOT / ".runtime/control.sqlite"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    load_env(args.env_file)
    backend = ("jev" if os.environ.get("TYPESAFE_API_KEY") else "offline") if args.backend == "auto" else args.backend
    gateway = AuthorizationGateway(SemanticReviewer(backend), AuditStore(args.db))
    app = create_control_app(gateway)
    print(f"\nTollgate · local financial sandbox\nBackend: {gateway.reviewer.model}")
    print(f"Open http://127.0.0.1:{args.port}/#token={app.state.operator_token}", flush=True)
    print("This URL grants local operator access. Keep it private. No real payments.\n", flush=True)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
