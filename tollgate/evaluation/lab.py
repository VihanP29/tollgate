"""Frozen experiments and shadows; evaluation workers receive a restricted repository."""
import copy
import time
from sqlalchemy import select
from tollgate.decision_context import DecisionInputV1, identity
from tollgate.evaluation.datasets import manifest, build_case, validate_manifest
from tollgate.evaluation.evaluator import evaluate_input, hard_checks, verdict
from tollgate.evaluation.metrics import summarize
from tollgate.policy_config import validate
from tollgate.services.platform import Conflict, new_id
from tollgate.storage import schema as s
from tollgate.storage.database import row, change


def create_experiment(platform, actor, wid, dataset_id, split, policy_ids):
    data = manifest()
    validate_manifest(data)
    if dataset_id != data["id"] or len(set(policy_ids)) != len(policy_ids):
        raise ValueError("Invalid dataset/configuration")
    with platform.db.transaction(wid) as c:
        platform.authorize(c, actor, wid, "lab")
        versions = [row(c, s.policies, wid, pid) for pid in policy_ids]
        cases = [{"metadata": case, "input": build_case(case, wid).model_dump(mode="json")} for case in data["cases"] if case["split"] == split]
        frozen = {"schema_version": 1, "dataset_id": dataset_id, "dataset_hash": data["manifest_hash"], "split": split,
                  "policies": {p["id"]: p["payload"] for p in versions}, "cases": cases,
                  "case_hashes": {case["metadata"]["id"]: identity(case["input"]) for case in cases},
                  "backend": "offline-fixture-checks", "compiler": "studio-1", "contract": "finance-1",
                  "arms": ["hard_constraints", "general_llm_unavailable"] + ["jev_fixture:"+p["id"] for p in versions],
                  "estimated_paid_calls": 0, "estimated_rows": len(cases)*(2+len(versions)),
                  "admission_eligible": False, "admission_block": "Unreviewed labels and fixture backend; live acceptance remains required"}
        eid = new_id("experiment")
        c.execute(s.experiments.insert().values(workspace_id=wid, id=eid, created_by=actor.actor_id, state="queued",
                  manifest_hash=identity(frozen), manifest=frozen, created_at=time.time()))
        platform.enqueue(c, wid, "experiment", eid, "experiment:"+eid)
        platform.audit(c, wid, actor.actor_id, "experiment_created", eid, {"manifest_hash": identity(frozen)})
        return row(c, s.experiments, wid, eid)


def experiment_result(platform, actor, wid, eid):
    with platform.db.transaction() as c:
        platform.authorize(c, actor, wid, "read")
        experiment = row(c, s.experiments, wid, eid)
        records = [r["payload"] for r in c.execute(select(s.experiment_rows).where(s.experiment_rows.c.workspace_id == wid,
                   s.experiment_rows.c.experiment_id == eid).order_by(s.experiment_rows.c.case_id, s.experiment_rows.c.arm)).mappings()]
    return {**experiment, "rows": records, "metrics": summarize(records), "completed_rows": len(records), "cost_usd": None}


class EvaluationRepository:
    """Capability boundary used by Lab: immutable reads and experiment-only result writes.

    This is an application boundary, not a claim of PostgreSQL privilege separation.
    """
    def __init__(self, database):
        self.__db = database

    def get(self, table, wid, rid):
        if table not in {s.experiments, s.inputs, s.policies, s.shadows, s.proposals}:
            raise ValueError("Evaluation cannot read that resource")
        with self.__db.transaction() as c:
            return row(c, table, wid, rid)

    def put_result(self, table, wid, values):
        if table not in {s.experiment_rows, s.shadow_rows}:
            raise ValueError("Evaluation cannot write operational state")
        fields = ("experiment_id", "case_id", "arm") if table == s.experiment_rows else ("subscription_id", "input_id")
        with self.__db.transaction(wid) as c:
            old = c.execute(select(table.c.id).where(table.c.workspace_id == wid, *(table.c[k] == values[k] for k in fields))).first()
            if not old:
                c.execute(table.insert().values(workspace_id=wid, id=new_id("result"), **values))

    def experiment_state(self, wid, eid, state):
        with self.__db.transaction(wid) as c:
            current = row(c, s.experiments, wid, eid)
            if current["state"] != "cancelled":
                change(c, s.experiments, wid, eid, state=state)


class Lab:
    def __init__(self, repository):
        self.store = repository

    def run(self, wid, eid):
        experiment = self.store.get(s.experiments, wid, eid)
        if experiment["state"] in {"cancelled", "completed"}:
            return
        frozen = experiment["manifest"]
        self.store.experiment_state(wid, eid, "running")
        for case in frozen["cases"]:
            for arm in frozen["arms"]:
                if self.store.get(s.experiments, wid, eid)["state"] == "cancelled":
                    return
                value = DecisionInputV1.model_validate(case["input"])
                hard = hard_checks(value)
                if arm == "hard_constraints":
                    result = {"verdict": verdict(hard), "checks": hard, "availability": "intentionally_not_evaluated", "model_calls": 0,
                              "note": "Experimental ablation; not an operational policy"}
                elif arm == "general_llm_unavailable":
                    result = {"verdict": "deny" if verdict(hard) == "deny" else "review", "checks": hard,
                              "availability": "not_required" if verdict(hard) == "deny" else "unavailable", "model_calls": 0,
                              "error_code": "general_llm_backend_not_configured"}
                else:
                    policy = frozen["policies"][arm.split(":", 1)[1]]
                    result = evaluate_input(value, policy)
                result.update(case_id=case["metadata"]["id"], family_id=case["metadata"]["family_id"],
                              expected=case["metadata"]["expected"], adjudication=case["metadata"]["adjudication"],
                              arm=arm, input_hash=value.input_hash, hard_pass=verdict(hard) != "deny")
                self.store.put_result(s.experiment_rows, wid, dict(experiment_id=eid, case_id=result["case_id"], arm=arm, payload=result))
        self.store.experiment_state(wid, eid, "completed")

    def shadow(self, wid, job):
        _, sid, input_id = job["logical_key"].split(":")
        subscription = self.store.get(s.shadows, wid, sid)
        capture = self.store.get(s.inputs, wid, input_id)
        value = DecisionInputV1.model_validate(capture["payload"])
        policy = self.store.get(s.policies, wid, subscription["policy_id"])["payload"]
        candidate = evaluate_input(value, policy)
        proposal = self.store.get(s.proposals, wid, capture["proposal_id"])
        candidate.update(active_verdict=proposal["verdict"], candidate_verdict=candidate["verdict"], input_hash=value.input_hash,
                         side_effect_free=True, new_provider_calls=0, backend="offline-fixture-checks")
        self.store.put_result(s.shadow_rows, wid, dict(subscription_id=sid, input_id=input_id, payload=candidate))


def subscribe_shadow(platform, actor, wid, policy_id):
    with platform.db.transaction(wid) as c:
        platform.authorize(c, actor, wid, "policy")
        row(c, s.policies, wid, policy_id)
        sid = new_id("shadow")
        c.execute(s.shadows.insert().values(workspace_id=wid, id=sid, policy_id=policy_id, enabled=True, created_at=time.time()))
        platform.audit(c, wid, actor.actor_id, "shadow_enabled", sid, {"policy_id": policy_id})
        return row(c, s.shadows, wid, sid)


def save_policy(platform, actor, wid, bundle):
    bundle = validate(bundle)
    with platform.db.transaction(wid) as c:
        platform.authorize(c, actor, wid, "policy")
        existing = row(c, s.policies, wid, bundle["id"], required=False)
        if existing:
            return existing
        c.execute(s.policies.insert().values(workspace_id=wid, id=bundle["id"], content_hash=bundle["hash"], payload=bundle,
                  created_at=time.time(), admission="untested"))
        platform.audit(c, wid, actor.actor_id, "policy_saved", bundle["id"], {"hash": bundle["hash"]})
        return row(c, s.policies, wid, bundle["id"])


def activate_policy(platform, actor, wid, policy_id, expected_revision):
    with platform.db.transaction() as c:
        platform.authorize(c, actor, wid, "policy")
        version = row(c, s.policies, wid, policy_id)
    bundle = version["payload"]
    # Four-case development regression only; no safety certification or live admission.
    results = []
    for scenario, expected in [("routine", "allow"), ("missing", "review" if bundle["require_confirmation"] else "allow"),
                               ("supplier", "review" if bundle["require_confirmation"] else "allow"), ("dispute", "deny")]:
        value = build_case({"scenario": scenario, "variant": 0}, wid, bundle)
        result = evaluate_input(value)
        # More restrictive deterministic auto-limit rules may add human review.
        if result["verdict"] == "review" and expected == "allow" and bundle["financial"]["auto_limit_cents"] < value.action.amount_minor:
            expected = "review"
        results.append({"scenario": scenario, "expected": expected, "observed": result["verdict"], "availability": result["availability"]})
    if not all(r["expected"] == r["observed"] and r["availability"] == "available" for r in results):
        raise Conflict("Offline development admission failed; edited prompts require live admission, which is unavailable")
    with platform.db.transaction(wid) as c:
        platform.authorize(c, actor, wid, "policy")
        workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id == wid)).mappings().one()
        if workspace["revision"] != expected_revision:
            raise Conflict("Workspace policy pointer revision changed")
        change(c, s.policies, wid, policy_id, admission="offline-fixture")
        c.execute(s.workspaces.update().where(s.workspaces.c.id == wid).values(active_policy_id=policy_id, revision=expected_revision+1))
        platform.audit(c, wid, actor.actor_id, "policy_activated", policy_id, {"backend": "offline-fixture", "regression": results})
        return {"policy_id": policy_id, "revision": expected_revision+1, "regression": results, "live_eligible": False}
