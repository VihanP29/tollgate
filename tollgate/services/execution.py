"""Durable dispatch and reconciliation. Unknown results never release financial claims."""
import time
from sqlalchemy import select
from tollgate.auth import Principal
from tollgate.decision_context import DecisionInputV1, ExecutorOutcome
from tollgate.evaluation.evaluator import hard_checks, verdict
from tollgate.executors import MockExecutor
from tollgate.services.platform import new_id
from tollgate.storage import schema as s
from tollgate.storage.database import row, change
from tollgate.observability import traced


class ExecutionService:
    def __init__(self, platform, executor=None):
        self.platform = platform
        self.db = platform.db
        self.executor = executor or MockExecutor()

    @traced("tollgate.executor.dispatch_or_reconcile")
    def execute(self, wid, intent_id):
        gateway = self.platform
        with self.db.transaction(wid) as c:
            intent = row(c, s.intents, wid, intent_id)
            if intent["state"] in {"confirmed", "failed", "cancelled"}:
                return
            proposal = row(c, s.proposals, wid, intent["proposal_id"])
            original = DecisionInputV1.model_validate(row(c, s.inputs, wid, proposal["input_id"])["payload"])
            current = gateway.capture(c, wid, proposal)
            reconcile = intent["state"] in {"dispatching", "submitted", "unknown"}
            if not reconcile:
                checks = hard_checks(current)
                workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id == wid)).mappings().one()
                valid = workspace["expires_at"] > time.time() and verdict(checks) != "deny" and gateway.binding(original) == gateway.binding(current)
                review = c.execute(select(s.reviews).where(s.reviews.c.workspace_id == wid,
                         s.reviews.c.proposal_id == proposal["id"])).mappings().first()
                if review:
                    valid = valid and review["state"] == "approved" and review["binding_hash"] == gateway.binding(current)
                    approvals = c.execute(select(s.resolutions.c.actor_id).where(s.resolutions.c.workspace_id == wid,
                        s.resolutions.c.review_id == review["id"], s.resolutions.c.resolution == "approve")).scalars().all()
                    valid = valid and len(set(approvals)) >= review["required_approvals"]
                    for actor in approvals:
                        if review["rule"].get("separation_of_duties") and actor == review["rule"]["mandate_creator"]:
                            valid = False
                        try:
                            gateway.authorize(c, Principal(actor), wid, "review")
                        except LookupError:
                            valid = False
                elif proposal["verdict"] != "allow":
                    valid = False
                if not valid:
                    gateway._cancel(c, wid, proposal, "dispatch_context_changed", review_state="stale")
                    return
                change(c, s.intents, wid, intent_id, state="dispatching", dispatched_at=time.time())
                change(c, s.proposals, wid, proposal["id"], state="dispatching")
                c.execute(s.attempts.insert().values(workspace_id=wid, id=new_id("attempt"), intent_id=intent_id,
                          created_at=time.time(), outcome="dispatching", payload={"provider_key": intent["provider_key"]}))
                gateway.audit(c, wid, "service:executor", "dispatch_started", intent_id)
        # Provider operations occur after the dispatch boundary commits.
        try:
            if reconcile and not self.executor.safe_retry:
                outcome = self.executor.lookup(intent["provider_key"], original.action)
            else:
                outcome = self.executor.dispatch(intent["provider_key"], original.action)
            outcome = ExecutorOutcome.model_validate(outcome)
        except Exception:
            outcome = ExecutorOutcome(state="unknown", error_code="dispatch_outcome_unknown")
        with self.db.transaction(wid) as c:
            intent = row(c, s.intents, wid, intent_id)
            if intent["state"] in {"confirmed", "failed", "cancelled"}:
                return
            proposal = row(c, s.proposals, wid, intent["proposal_id"])
            c.execute(s.attempts.insert().values(workspace_id=wid, id=new_id("attempt"), intent_id=intent_id,
                created_at=time.time(), outcome=outcome.state, payload=outcome.model_dump(mode="json")))
            change(c, s.intents, wid, intent_id, state=outcome.state, operation_id=outcome.operation_id, last_error=outcome.error_code)
            change(c, s.proposals, wid, proposal["id"], state=outcome.state, updated_at=time.time())
            if outcome.state == "confirmed":
                if not outcome.receipt or not outcome.operation_id:
                    raise ValueError("Confirmation requires an authoritative receipt and operation ID")
                c.execute(s.receipts.insert().values(workspace_id=wid, id=new_id("receipt"), proposal_id=proposal["id"], intent_id=intent_id,
                    created_at=time.time(), payload=outcome.receipt))
                gateway._release(c, wid, proposal, "execution_confirmed", committed=True)
            elif outcome.state == "failed":
                gateway._release(c, wid, proposal, "known_nonexecution")
            gateway.audit(c, wid, "service:executor", "execution_" + outcome.state, intent_id,
                          {"operation_id": outcome.operation_id, "error_code": outcome.error_code})
