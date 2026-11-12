"""A small persisted fixture runner, including outcomes across human review waits."""
import time
from sqlalchemy import select
from tollgate.auth import Principal
from tollgate.decision_context import ProposalV1, identity, utcnow
from tollgate.services.platform import Conflict, new_id
from tollgate.storage import schema as s
from tollgate.storage.database import row, change


def start_run(platform, actor, wid, mid, request_id):
    with platform.db.transaction(wid) as c:
        platform.authorize(c, actor, wid, "operate")
        mandate = row(c, s.mandates, wid, mid)
        if mandate["revoked"] or mandate["expires_at"] <= time.time():
            raise Conflict("Mandate unavailable")
        existing = c.execute(select(s.runs).where(s.runs.c.workspace_id == wid, s.runs.c.mandate_id == mid,
                            s.runs.c.request_id == request_id)).mappings().first()
        if existing:
            return dict(existing)
        rid = new_id("run")
        c.execute(s.runs.insert().values(workspace_id=wid, id=rid, mandate_id=mid, principal_id="service:"+rid,
            request_id=request_id, state="running", revision=1, step=0, created_at=time.time(), payload={"outcomes": {}, "mode": "scripted-fixtures"}))
        platform.enqueue(c, wid, "run", rid, "run:"+rid+":start")
        platform.audit(c, wid, actor.actor_id, "run_started", rid, {"mode": "scripted-fixtures"})
        return row(c, s.runs, wid, rid)


def advance_run(platform, wid, rid):
    with platform.db.transaction(wid) as c:
        run = row(c, s.runs, wid, rid)
        if run["state"] in {"cancelled", "finished", "failed", "step_limit"}:
            return
        mandate = row(c, s.mandates, wid, run["mandate_id"])
        if mandate["revoked"] or mandate["expires_at"] <= time.time():
            change(c, s.runs, wid, rid, state="cancelled")
            return
        approved = mandate["payload"]["authority"]["approved_invoices"]
    actor = Principal(run["principal_id"], "service", workspace_id=wid, mandate_id=run["mandate_id"])
    outcomes = {}
    for iid, invoice in approved.items():
        if len(outcomes) >= platform.settings.max_steps:
            break
        # Stable ID and date survive restart; use the persisted creation date, never today's date on retry.
        from datetime import datetime, timezone
        day = datetime.fromtimestamp(run["created_at"], timezone.utc).date().isoformat()
        body = ProposalV1.model_validate({"request_id": rid+":"+iid, "action": dict(tool="schedule_payment", invoice_id=iid,
            amount_minor=invoice["amount_cents"], currency="USD", beneficiary_ref=invoice["vendor_id"], scheduled_date=day)})
        result = platform.submit(actor, wid, run["mandate_id"], body)
        outcomes[iid] = {"proposal_id": result["id"], "state": result["state"], "receipt_id": result["receipt_id"]}
    states = {r["state"] for r in outcomes.values()}
    state = ("step_limit" if len(outcomes) < len(approved) else "waiting_for_review" if "awaiting_review" in states
             else "waiting" if states & {"evaluating", "queued", "dispatching", "submitted", "unknown"} else "finished")
    with platform.db.transaction(wid) as c:
        current = row(c, s.runs, wid, rid)
        if current["state"] == "cancelled":
            return
        change(c, s.runs, wid, rid, state=state, step=len(outcomes), revision=current["revision"]+1,
               payload={"outcomes": outcomes, "mode": "scripted-fixtures", "all_paid": all(r["state"] == "confirmed" for r in outcomes.values())})
        platform.audit(c, wid, "service:runner", "run_checkpoint", rid, {"state": state, "processed": len(outcomes)})


def wake_runs(platform):
    with platform.db.transaction() as c:
        waiting = [dict(r) for r in c.execute(select(s.runs).where(s.runs.c.state.in_(["waiting", "waiting_for_review"])).limit(100)).mappings()]
    for run in waiting:
        wid = run["workspace_id"]
        with platform.db.transaction(wid) as c:
            current = row(c, s.runs, wid, run["id"])
            outcomes = current["payload"]["outcomes"]
            changed = any(row(c, s.proposals, wid, r["proposal_id"])["state"] != r["state"] for r in outcomes.values())
            if changed:
                platform.enqueue(c, wid, "run", run["id"], f"run:{run['id']}:{current['revision']}")
