"""Thin versioned HTTP adapter for the relational application services."""
from __future__ import annotations

import time
from typing import Annotated, Literal
from fastapi import Depends, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, StrictInt
from sqlalchemy import select, func
from tollgate.authorization import ROOT
from tollgate.decision_context import Identifier, ProposalV1, StrictModel
from tollgate.services.platform import Conflict, QuotaExceeded
from tollgate.storage import schema as s
from tollgate.storage.database import row, change


class Provision(StrictModel):
    request_id: Identifier


class MandateRequest(StrictModel):
    invoice_ids: Annotated[list[Identifier], Field(min_length=1, max_length=20)]
    budget_minor: Annotated[StrictInt, Field(gt=0, le=1000000000)] = 6000000
    auto_limit_minor: Annotated[StrictInt, Field(ge=0, le=1000000000)] = 1000000
    expires_seconds: Annotated[StrictInt, Field(ge=60, le=7200)] = 7200
    required_approvals: Annotated[StrictInt, Field(ge=1, le=5)] = 1
    reviewers: Annotated[list[str], Field(max_length=5)] | None = None


class CredentialRequest(StrictModel):
    mandate_id: Identifier
    expires_seconds: Annotated[StrictInt, Field(ge=60, le=7200)] = 3600


class EvidenceRequest(StrictModel):
    body: Annotated[str, Field(min_length=1, max_length=6000)]
    source_type: Literal["supplier", "operator_note", "verified_receiving"]
    supersedes_id: Identifier | None = None


class ResolutionRequest(StrictModel):
    resolution: Literal["approve", "reject"]
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    comment: Annotated[str, Field(max_length=2000)] = ""


class ExperimentRequest(StrictModel):
    dataset_id: Literal["synthetic-v1"] = "synthetic-v1"
    split: Literal["development", "held_out"] = "development"
    policy_ids: Annotated[list[Identifier], Field(min_length=1, max_length=3)]


class ShadowRequest(StrictModel):
    policy_id: Identifier


class ToggleRequest(StrictModel):
    enabled: bool


class PolicyRequest(StrictModel):
    bundle: dict


class ActivationRequest(StrictModel):
    policy_id: Identifier
    expected_revision: Annotated[StrictInt, Field(ge=1)]


class RevisionRequest(StrictModel):
    expected_revision: Annotated[StrictInt, Field(ge=1)]


class MemberRequest(StrictModel):
    actor_id: Annotated[str, Field(min_length=1, max_length=200)]
    role: Literal["admin", "operator", "reviewer", "auditor"]
    active: bool = True


def install(app, platform):
    app.state.platform = platform

    @app.exception_handler(LookupError)
    async def missing(_, exc):
        return JSONResponse({"error": {"code": "resource_unavailable"}}, status_code=404)

    @app.exception_handler(PermissionError)
    async def unauthenticated(_, exc):
        return JSONResponse({"error": {"code": "authentication_required"}}, status_code=401)

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse({"error": {"code": "conflict", "message": str(exc)}}, status_code=409)

    @app.exception_handler(QuotaExceeded)
    async def quota(_, exc):
        return JSONResponse({"error": {"code": "quota_exhausted", "message": str(exc)}}, status_code=429)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Request is invalid for the current configuration"}}, status_code=422)

    def principal(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise PermissionError("Authentication required")
        return platform.identity.authenticate(authorization[7:])

    @app.get("/health/live")
    def live():
        return {"ok": True}

    @app.get("/health/ready")
    def ready():
        try:
            db_ready = platform.db.ready()
            with platform.db.transaction() as c:
                heartbeat = c.execute(select(func.max(s.workers.c.heartbeat_at))).scalar_one()
            worker_ready = bool(heartbeat and heartbeat > time.time() - platform.settings.worker_stale_seconds)
            return JSONResponse({"database": db_ready, "worker": worker_ready, "backend": "offline-fixture-checks",
                "live_jev": False, "accounting": False}, status_code=200 if db_ready and worker_ready else 503)
        except Exception:
            return JSONResponse({"database": False, "worker": False}, status_code=503)

    @app.get("/workspace")
    def workspace_page():
        return FileResponse(ROOT / "dashboard/workspace.html")

    @app.get("/platform/{asset}")
    def asset(asset: str):
        if asset not in {"workspace.js", "workspace.css", "api.js", "auth.js", "shell.js"}:
            raise HTTPException(404)
        return FileResponse(ROOT / "dashboard" / asset)

    @app.get("/api/v1/me")
    def me(p=Depends(principal)):
        return {"actor_id": p.actor_id, "kind": p.kind, "mode": platform.settings.mode,
                "backend": "offline-fixture-checks", "live_execution": False}

    @app.get("/api/v1/workspaces")
    def workspaces(p=Depends(principal)):
        return platform.workspaces(p)

    @app.post("/api/v1/demo-workspaces")
    def provision(body: Provision, p=Depends(principal)):
        return platform.provision(p, body.request_id)

    prefix = "/api/v1/workspaces/{wid}"

    @app.post(prefix + "/reset")
    def reset(wid: str, body: RevisionRequest, p=Depends(principal)):
        return platform.reset_synthetic(p, wid, body.expected_revision)

    @app.get(prefix + "/mandates")
    def mandates(wid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.mandates)

    @app.post(prefix + "/mandates")
    def mandate(wid: str, body: MandateRequest, p=Depends(principal)):
        return platform.create_mandate(p, wid, **body.model_dump())

    @app.get(prefix + "/mandates/{mid}")
    def mandate_detail(wid: str, mid: str, p=Depends(principal)):
        with platform.db.transaction() as c:
            platform.authorize(c, p, wid, "read")
            return row(c, s.mandates, wid, mid)

    @app.post(prefix + "/mandates/{mid}/revoke")
    def revoke_mandate(wid: str, mid: str, p=Depends(principal)):
        platform.revoke_mandate(p, wid, mid)
        return {"revoked": True, "already_dispatched": "Requires reconciliation; reservations retained"}

    @app.get(prefix + "/mandates/{mid}/invoices")
    def invoices(wid: str, mid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.invoices, mid)

    @app.get(prefix + "/mandates/{mid}/vendors")
    def vendors(wid: str, mid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.vendors, mid)

    @app.get(prefix + "/invoices")
    def catalog(wid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.invoices)

    @app.post(prefix + "/mandates/{mid}/proposals")
    def propose(wid: str, mid: str, body: ProposalV1, p=Depends(principal)):
        result = platform.submit(p, wid, mid, body)
        return JSONResponse(result, status_code=202 if result["state"] in {"evaluating", "awaiting_review", "queued"} else 200)

    @app.get(prefix + "/proposals")
    def proposals(wid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.proposals)

    @app.get(prefix + "/proposals/{pid}")
    def proposal(wid: str, pid: str, p=Depends(principal)):
        return platform.proposal(p, wid, pid)

    @app.post(prefix + "/proposals/{pid}/reassess")
    def reassess(wid: str, pid: str, p=Depends(principal)):
        return platform.reassess(p, wid, pid)

    @app.get(prefix + "/receipts/{rid}")
    def receipt(wid: str, rid: str, p=Depends(principal)):
        return platform.receipt(p, wid, rid)

    @app.get(prefix + "/agent-credentials")
    def credentials(wid: str, p=Depends(principal)):
        with platform.db.transaction() as c:
            platform.authorize(c, p, wid, "operate")
        return platform.list_resources(p, wid, s.credentials)

    @app.post(prefix + "/agent-credentials")
    def issue(wid: str, body: CredentialRequest, p=Depends(principal)):
        return platform.issue_credential(p, wid, body.mandate_id, body.expires_seconds)

    @app.post(prefix + "/agent-credentials/{cid}/revoke")
    def revoke(wid: str, cid: str, p=Depends(principal)):
        platform.revoke_credential(p, wid, cid)
        return {"revoked": True, "accepted_work_cancelled": False}

    @app.get(prefix + "/reviews")
    def reviews(wid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.reviews)

    @app.get(prefix + "/reviews/{rid}")
    def review(wid: str, rid: str, p=Depends(principal)):
        with platform.db.transaction() as c:
            platform.authorize(c, p, wid, "read")
            result = row(c, s.reviews, wid, rid)
            result["resolutions"] = [dict(r) for r in c.execute(select(s.resolutions).where(s.resolutions.c.workspace_id == wid,
                s.resolutions.c.review_id == rid)).mappings()]
            return result

    @app.post(prefix + "/reviews/{rid}/resolve")
    def resolve(wid: str, rid: str, body: ResolutionRequest, p=Depends(principal)):
        return platform.resolve(p, wid, rid, **body.model_dump())

    @app.get(prefix + "/invoices/{iid}/evidence")
    def evidence(wid: str, iid: str, p=Depends(principal)):
        with platform.db.transaction() as c:
            platform.authorize(c, p, wid, "read")
            row(c, s.invoices, wid, iid)
            return [dict(r) for r in c.execute(select(s.evidence).where(s.evidence.c.workspace_id == wid,
                s.evidence.c.invoice_id == iid).order_by(s.evidence.c.revision)).mappings()]

    @app.post(prefix + "/invoices/{iid}/evidence")
    def add_evidence(wid: str, iid: str, body: EvidenceRequest, p=Depends(principal)):
        return platform.append_evidence(p, wid, iid, **body.model_dump())

    @app.get(prefix + "/runs")
    def runs(wid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.runs)

    @app.post(prefix + "/mandates/{mid}/runs")
    def run(wid: str, mid: str, body: Provision, p=Depends(principal)):
        from tollgate.services.runs import start_run
        return start_run(platform, p, wid, mid, body.request_id)

    @app.post(prefix + "/runs/{rid}/cancel")
    def cancel_run(wid: str, rid: str, p=Depends(principal)):
        with platform.db.transaction(wid) as c:
            platform.authorize(c, p, wid, "operate")
            record = row(c, s.runs, wid, rid)
            change(c, s.runs, wid, rid, state="cancelled", revision=record["revision"] + 1)
            # Cancellation explicitly cancels undispatched proposals accepted by this run.
            for proposal in c.execute(select(s.proposals).where(s.proposals.c.workspace_id == wid,
                       s.proposals.c.principal_id == record["principal_id"])).mappings().all():
                if proposal["state"] in {"evaluating", "queued", "awaiting_review", "held"}:
                    platform._cancel(c, wid, dict(proposal), "run_cancelled")
            platform.audit(c, wid, p.actor_id, "run_cancelled", rid)
            return {"state": "cancelled"}

    @app.post(prefix + "/memberships")
    def member(wid: str, body: MemberRequest, p=Depends(principal)):
        with platform.db.transaction(wid) as c:
            platform.authorize(c, p, wid, "admin")
            old = c.execute(select(s.memberships).where(s.memberships.c.workspace_id == wid,
                         s.memberships.c.actor_id == body.actor_id)).mappings().first()
            if old and old["role"] == "owner":
                raise Conflict("Owner membership is protected")
            values = body.model_dump()
            if old:
                c.execute(s.memberships.update().where(s.memberships.c.workspace_id == wid,
                    s.memberships.c.actor_id == body.actor_id).values(role=body.role, active=body.active))
            else:
                c.execute(s.memberships.insert().values(workspace_id=wid, **values))
            platform.audit(c, wid, p.actor_id, "membership_changed", wid, values)
            return values

    @app.get(prefix + "/policies")
    def policies(wid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.policies)

    @app.post(prefix + "/policies")
    def save_policy(wid: str, body: PolicyRequest, p=Depends(principal)):
        from tollgate.evaluation.lab import save_policy
        return save_policy(platform, p, wid, body.bundle)

    @app.post(prefix + "/policies/activate")
    def activate(wid: str, body: ActivationRequest, p=Depends(principal)):
        from tollgate.evaluation.lab import activate_policy
        return activate_policy(platform, p, wid, **body.model_dump())

    @app.get(prefix + "/datasets")
    def datasets(wid: str, p=Depends(principal)):
        from tollgate.evaluation.datasets import manifest
        with platform.db.transaction() as c:
            platform.authorize(c, p, wid, "read")
        data = manifest()
        return [{k: v for k, v in data.items() if k != "cases"}]

    @app.post(prefix + "/experiments")
    def experiment(wid: str, body: ExperimentRequest, p=Depends(principal)):
        from tollgate.evaluation.lab import create_experiment
        return create_experiment(platform, p, wid, **body.model_dump())

    @app.get(prefix + "/experiments")
    def experiments(wid: str, p=Depends(principal)):
        return platform.list_resources(p, wid, s.experiments)

    @app.get(prefix + "/experiments/{eid}")
    def experiment_results(wid: str, eid: str, p=Depends(principal)):
        from tollgate.evaluation.lab import experiment_result
        return experiment_result(platform, p, wid, eid)

    @app.post(prefix + "/experiments/{eid}/cancel")
    def cancel_experiment(wid: str, eid: str, p=Depends(principal)):
        with platform.db.transaction(wid) as c:
            platform.authorize(c, p, wid, "lab")
            row(c, s.experiments, wid, eid)
            change(c, s.experiments, wid, eid, state="cancelled")
            platform.audit(c, wid, p.actor_id, "experiment_cancelled", eid)
            return {"state": "cancelled"}

    @app.get(prefix + "/shadow-policies")
    def shadows(wid: str, p=Depends(principal)):
        return {"subscriptions": platform.list_resources(p, wid, s.shadows), "results": platform.list_resources(p, wid, s.shadow_rows)}

    @app.post(prefix + "/shadow-policies")
    def shadow(wid: str, body: ShadowRequest, p=Depends(principal)):
        from tollgate.evaluation.lab import subscribe_shadow
        return subscribe_shadow(platform, p, wid, body.policy_id)

    @app.patch(prefix + "/shadow-policies/{sid}")
    def toggle_shadow(wid: str, sid: str, body: ToggleRequest, p=Depends(principal)):
        with platform.db.transaction(wid) as c:
            platform.authorize(c, p, wid, "policy")
            row(c, s.shadows, wid, sid)
            change(c, s.shadows, wid, sid, enabled=body.enabled)
            platform.audit(c, wid, p.actor_id, "shadow_toggled", sid, {"enabled": body.enabled})
            return {"id": sid, "enabled": body.enabled}

    @app.get(prefix + "/audit")
    def audit(wid: str, limit: int = 100, after: float = 0, p=Depends(principal)):
        if not 1 <= limit <= 200 or after < 0:
            raise ValueError("Invalid pagination")
        with platform.db.transaction() as c:
            platform.authorize(c, p, wid, "read")
            records = [dict(r) for r in c.execute(select(s.audit).where(s.audit.c.workspace_id == wid,
                 s.audit.c.created_at > after).order_by(s.audit.c.created_at, s.audit.c.id).limit(limit)).mappings()]
            return {"schema_version": 1, "events": records, "next_after": records[-1]["created_at"] if records else None,
                    "integrity": "Content hashes identify payloads; no signature or independent anchoring"}

    @app.get(prefix + "/connections")
    def connections(wid: str, p=Depends(principal)):
        with platform.db.transaction() as c:
            platform.authorize(c, p, wid, "read")
        return {"environment": "synthetic", "accounting_available": False, "writeback": False,
                "reason": "QuickBooks sandbox OAuth, encrypted token storage, and reconciliation are not implemented"}
