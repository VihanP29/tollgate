"""Persisted bounded workers. Leases deduplicate work; executor recovery uses its own contract."""
import argparse
import time
import signal
import threading
from pathlib import Path
from sqlalchemy import select, or_, and_, func
from tollgate.decision_context import DecisionInputV1
from tollgate.services.execution import ExecutionService
from tollgate.services.platform import Platform, new_id
from tollgate.storage.database import Database, row, change
from tollgate.storage import schema as s
from tollgate.settings import Settings


class Worker:
    def __init__(self, platform, executor=None, worker_id=None):
        self.platform = platform
        self.db = platform.db
        self.execution = ExecutionService(platform, executor)
        self.id = worker_id or new_id("worker")

    def heartbeat(self):
        with self.db.transaction() as c:
            old = c.execute(select(s.workers).where(s.workers.c.id == self.id)).first()
            if old:
                c.execute(s.workers.update().where(s.workers.c.id == self.id).values(heartbeat_at=time.time()))
            else:
                c.execute(s.workers.insert().values(id=self.id, heartbeat_at=time.time()))

    def claim(self):
        self.heartbeat()
        with self.db.transaction() as c:
            now = time.time()
            # Database-serialized quota admission, never a process-local lock.
            c.execute(select(s.usage).where(s.usage.c.scope == "global").with_for_update()).first()
            active = c.execute(select(func.count()).select_from(s.jobs).where(s.jobs.c.state == "running",
                               s.jobs.c.lease_until >= now)).scalar_one()
            if active >= self.platform.settings.global_jobs:
                return None
            running = s.jobs.alias("running")
            pool = ["shadow", "experiment"]
            same_pool = running.c.kind.in_(pool) == s.jobs.c.kind.in_(pool)
            workspace_active = select(func.count()).select_from(running).where(
                running.c.workspace_id == s.jobs.c.workspace_id, running.c.state == "running",
                running.c.lease_until >= now, same_pool).correlate(s.jobs).scalar_subquery()
            job = c.execute(select(s.jobs).where(workspace_active < self.platform.settings.max_jobs, or_(and_(s.jobs.c.state == "queued", s.jobs.c.available_at <= now),
                and_(s.jobs.c.state == "running", s.jobs.c.lease_until < now))).order_by(
                    # Shadow/experiments cannot starve operational work.
                    s.jobs.c.kind.in_(["shadow", "experiment"]), s.jobs.c.available_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not job:
                return None
            job = dict(job)
            if job["cancel_requested"]:
                change(c, s.jobs, job["workspace_id"], job["id"], state="cancelled")
                return None
            if job["attempts"] >= self.platform.settings.max_retries + 1:
                change(c, s.jobs, job["workspace_id"], job["id"], state="failed", last_error="attempt_limit")
                return None
            change(c, s.jobs, job["workspace_id"], job["id"], state="running", attempts=job["attempts"] + 1,
                   lease_owner=self.id, lease_until=now + self.platform.settings.lease_seconds)
            job.update(lease_owner=self.id, attempts=job["attempts"] + 1)
            return job

    def tick(self):
        from tollgate.services.runs import wake_runs
        wake_runs(self.platform)
        job = self.claim()
        if not job:
            return False
        wid = job["workspace_id"]
        try:
            if job["kind"] == "evaluate":
                self.platform.evaluate_proposal(wid, job["object_id"])
            elif job["kind"] == "execute":
                self.execution.execute(wid, job["object_id"])
            elif job["kind"] in {"experiment", "shadow"}:
                from tollgate.evaluation.lab import Lab, EvaluationRepository
                lab = Lab(EvaluationRepository(self.db))
                if job["kind"] == "experiment":
                    lab.run(wid, job["object_id"])
                else:
                    lab.shadow(wid, job)
            elif job["kind"] == "run":
                from tollgate.services.runs import advance_run
                advance_run(self.platform, wid, job["object_id"])
            else:
                raise ValueError("Unsupported job type")
            with self.db.transaction(wid) as c:
                # A worker whose lease was stolen cannot acknowledge another owner's work.
                current = row(c, s.jobs, wid, job["id"])
                if current["lease_owner"] == self.id:
                    change(c, s.jobs, wid, job["id"], state="completed", lease_until=None, last_error=None)
            return True
        except Exception as exc:
            error_code = "job_handler_failed:" + type(exc).__name__
            with self.db.transaction(wid) as c:
                current = row(c, s.jobs, wid, job["id"])
                if current["lease_owner"] == self.id:
                    exhausted = current["attempts"] >= self.platform.settings.max_retries + 1
                    change(c, s.jobs, wid, job["id"], state="failed" if exhausted else "queued", lease_until=None,
                           available_at=time.time()+min(30, 2**current["attempts"]), last_error=error_code)
                    if exhausted and job["kind"] == "evaluate":
                        proposal = row(c, s.proposals, wid, job["object_id"])
                        if proposal["state"] == "evaluating":
                            change(c, s.proposals, wid, proposal["id"], state="held", verdict="review", checks=[dict(
                                id="evaluation_job_failed", kind="semantic", outcome="unavailable", label="Evaluation worker failed",
                                detail="Bounded job retries exhausted; inspect jobs and explicitly reassess")])
                    elif exhausted and job["kind"] == "experiment":
                        experiment = row(c, s.experiments, wid, job["object_id"])
                        if experiment["state"] != "cancelled":
                            change(c, s.experiments, wid, experiment["id"], state="partial_failure")
                    elif exhausted and job["kind"] == "execute":
                        intent = row(c, s.intents, wid, job["object_id"])
                        if intent["state"] in {"dispatching", "submitted"}:
                            change(c, s.intents, wid, intent["id"], state="unknown", last_error="worker_recovery_required")
                            change(c, s.proposals, wid, intent["proposal_id"], state="unknown")
                    self.platform.audit(c, wid, "service:worker", "job_failed", job["id"], {"exhausted": exhausted})
            return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parents[2] / ".env"))
    args = parser.parse_args()
    from tollgate.settings import load_env
    load_env(args.env_file)
    from tollgate.observability import configure_logging
    configure_logging()
    settings = Settings.from_env()
    db = Database(settings.database_url)
    if not db.ready():
        raise SystemExit("Run alembic upgrade head first")
    worker = Worker(Platform(db, settings))
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        while not stopping.is_set():
            worked = worker.tick()
            if args.once:
                break
            if not worked:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
