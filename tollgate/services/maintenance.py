"""Bounded recovery of expired authority and exhausted durable jobs."""
import time
from sqlalchemy import and_, or_, select
from tollgate.storage import schema as s
from tollgate.storage.database import row, change

UNDISPATCHED = ("evaluating", "awaiting_review", "queued", "held")
ACTIVE_RUNS = ("running", "waiting", "waiting_for_review")


def expire_authority(platform):
    """Cancel only known undispatched work; ambiguous submissions keep their claims."""
    expired = or_(s.mandates.c.expires_at <= time.time(), s.mandates.c.revoked.is_(True),
                  s.workspaces.c.expires_at <= time.time())
    mandate_join = and_(s.mandates.c.workspace_id == s.proposals.c.workspace_id,
                       s.mandates.c.id == s.proposals.c.mandate_id)
    run_join = and_(s.mandates.c.workspace_id == s.runs.c.workspace_id,
                   s.mandates.c.id == s.runs.c.mandate_id)
    with platform.db.transaction() as c:
        proposals = select(s.proposals.c.workspace_id).join(s.mandates, mandate_join).join(
            s.workspaces, s.workspaces.c.id == s.proposals.c.workspace_id).where(
            s.proposals.c.state.in_(UNDISPATCHED), expired).distinct().limit(100)
        runs = select(s.runs.c.workspace_id).join(s.mandates, run_join).join(
            s.workspaces, s.workspaces.c.id == s.runs.c.workspace_id).where(
            s.runs.c.state.in_(ACTIVE_RUNS), expired).distinct().limit(100)
        workspaces = set(c.execute(proposals).scalars()) | set(c.execute(runs).scalars())
    changed = False
    for wid in workspaces:
        with platform.db.transaction(wid) as c:
            workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id == wid)).mappings().one()
            now = time.time()
            query = select(s.mandates).where(s.mandates.c.workspace_id == wid)
            if workspace["expires_at"] > now:
                query = query.where(or_(s.mandates.c.expires_at <= now, s.mandates.c.revoked.is_(True)))
            for mandate in c.execute(query).mappings().all():
                reason = ("workspace_expired" if workspace["expires_at"] <= now else
                          "mandate_revoked" if mandate["revoked"] else "mandate_expired")
                proposals = c.execute(select(s.proposals).where(s.proposals.c.workspace_id == wid,
                    s.proposals.c.mandate_id == mandate["id"], s.proposals.c.state.in_(UNDISPATCHED))).mappings().all()
                for proposal in proposals:
                    changed |= platform._cancel(c, wid, dict(proposal), reason,
                                               review_state="cancelled" if reason == "mandate_revoked" else "expired")
                stopped = c.execute(s.runs.update().where(s.runs.c.workspace_id == wid,
                    s.runs.c.mandate_id == mandate["id"], s.runs.c.state.in_(ACTIVE_RUNS)).values(
                        state="cancelled", revision=s.runs.c.revision + 1)).rowcount
                if stopped:
                    platform.audit(c, wid, "service:maintenance", "runs_authority_expired", mandate["id"], {"count": stopped})
                    changed = True
    return changed


def finish_exhausted_job(platform, c, wid, job):
    if job["kind"] == "evaluate":
        proposal = row(c, s.proposals, wid, job["object_id"])
        if proposal["state"] == "evaluating":
            change(c, s.proposals, wid, proposal["id"], state="held", verdict="review", checks=[dict(
                id="evaluation_job_failed", kind="semantic", outcome="unavailable", label="Evaluation worker failed",
                detail="Bounded job attempts exhausted; inspect jobs and explicitly reassess")])
    elif job["kind"] == "experiment":
        experiment = row(c, s.experiments, wid, job["object_id"])
        if experiment["state"] != "cancelled":
            change(c, s.experiments, wid, experiment["id"], state="partial_failure")
    elif job["kind"] == "execute":
        intent = row(c, s.intents, wid, job["object_id"])
        if intent["state"] in {"dispatching", "submitted"}:
            change(c, s.intents, wid, intent["id"], state="unknown", last_error="worker_recovery_required")
            change(c, s.proposals, wid, intent["proposal_id"], state="unknown")
        elif intent["state"] == "queued":
            platform._cancel(c, wid, row(c, s.proposals, wid, intent["proposal_id"]), "execution_job_exhausted")
    elif job["kind"] == "run":
        run = row(c, s.runs, wid, job["object_id"])
        if run["state"] in ACTIVE_RUNS:
            change(c, s.runs, wid, run["id"], state="failed", revision=run["revision"] + 1)


def recover_exhausted_jobs(platform):
    # The failed-job marker and this follow-up survive a crash between units of work.
    with platform.db.transaction() as c:
        pending = [dict(j) for j in c.execute(select(s.jobs).where(s.jobs.c.state == "failed",
                    s.jobs.c.last_error == "attempt_limit").limit(100)).mappings()]
    for job in pending:
        wid = job["workspace_id"]
        with platform.db.transaction(wid) as c:
            current = row(c, s.jobs, wid, job["id"])
            if current["state"] != "failed" or current["last_error"] != "attempt_limit":
                continue
            finish_exhausted_job(platform, c, wid, current)
            change(c, s.jobs, wid, job["id"], last_error="attempt_limit_recovered")
            platform.audit(c, wid, "service:maintenance", "job_attempts_exhausted", job["id"])
    return bool(pending)
