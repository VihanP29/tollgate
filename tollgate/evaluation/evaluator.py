"""Execution-free canonical evaluator. It has no repositories or executor capability."""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from tollgate.authorization import Proposal
from tollgate.decision_context import DecisionInputV1, Payment, identity
from tollgate.financial import check_constraints
from tollgate.semantic_policy import evaluate
from tollgate.policy_config import request


def hard_checks(value: DecisionInputV1, policy=None):
    mandate = value.unpack("authority")
    financial_policy = (policy or value.unpack("policy"))["financial"]
    mandate["budget_cents"] = min(mandate["budget_cents"], financial_policy["budget_cents"])
    mandate["auto_limit_cents"] = min(mandate["auto_limit_cents"], financial_policy["auto_limit_cents"])
    mandate["expires_at"] = datetime.fromisoformat(mandate["expires_at"])
    state = value.unpack("financial")
    sources = value.unpack("sources")
    action = value.action
    if isinstance(action, Payment):
        args = dict(memo=action.invoice_id, vendor_id=action.beneficiary_ref,
                    amount_usd=str(Decimal(action.amount_minor) / 100), date=action.scheduled_date.isoformat())
    else:
        args = action.model_dump(exclude={"tool"})
    session = SimpleNamespace(mandate=SimpleNamespace(**mandate), vendors=sources["vendors"],
                              ledger=[{"invoice_id": action.invoice_id}] if state["duplicate"] else [], events=[])
    checks = [c.model_dump() for c in check_constraints(session, Proposal(tool=action.tool, args=args),
                state["spent_minor"], state["reserved_minor"], evaluation_time=value.evaluation_time)]
    if isinstance(action, Payment) and sources.get("source_revision") != sources.get("pinned_source_revision"):
        checks.append(dict(id="source_revision", label="Invoice source changed after authorization", kind="constraint",
                           outcome="fail", detail="Create fresh authority against the revised source; pinned snapshots are immutable"))
    return checks


def semantic_state(value):
    sources = value.unpack("sources")
    # All applicable adverse records remain present. Superseded records stay in the capture.
    records = value.unpack("evidence")
    superseded = {r.get("supersedes_id") for r in records if r["source_type"] == "verified_receiving"}
    return dict(proposed_action=value.action.model_dump(mode="json"),
                operator_request=value.unpack("authority")["task"],
                invoice_text=[i["text"] for i in sources["invoices"].values()],
                evidence=[r for r in records if r["evidence_id"] not in superseded
                          and r.get("invoice_id") == getattr(value.action, "invoice_id", None)],
                evidence_revision=value.evidence_revision,
                financial_state=value.unpack("financial"),
                approved_invoices=value.unpack("authority")["approved_invoices"],
                approved_beneficiary_refs=list(value.unpack("authority")["beneficiaries"]),
                source_revision=sources.get("source_revision"),
                authority_revision=value.mandate_revision,
                claims=[c.model_dump(mode="json") for c in value.claims])


def compiled_request(value, policy=None):
    return request(policy or value.unpack("policy"), semantic_state(value))


def request_identity(value, policy=None):
    return identity({"request": compiled_request(value, policy), "normalization": value.normalization_version,
                     "compiler": value.compiler_version, "retrieval": value.retrieval_version,
                     "contract": value.semantic_contract, "assets": []})


def verdict(checks):
    return "deny" if any(c["outcome"] == "fail" for c in checks) else "review" if any(
        c["outcome"] in ("review", "unavailable") for c in checks) else "allow"


def evaluate_input(value: DecisionInputV1, policy=None, backend="offline", client=None):
    hard = hard_checks(value, policy)
    if verdict(hard) == "deny" or not isinstance(value.action, Payment):
        return dict(verdict=verdict(hard), checks=hard, availability="not_required", model_calls=0,
                    usage={}, provider_latency_ms=None, evaluation=None, request_hash=request_identity(value, policy))
    policy = policy or value.unpack("policy")
    try:
        checks, data = evaluate(client, semantic_state(value), policy, offline=backend == "offline")
        if data.get("request_hash"):
            data["provider_payload_hash"] = data["request_hash"]
        data.update(request_hash=request_identity(value, policy), requested_model=policy["model"], resolved_model=data["model"])
        return dict(verdict=verdict(hard + checks), checks=hard + checks, availability="available",
                    model_calls=int(backend != "offline"), usage=data["usage"],
                    provider_latency_ms=data["latency_ms"] if backend != "offline" else None, evaluation=data, request_hash=request_identity(value, policy))
    except Exception:
        checks = hard + [dict(id="classifier_available", label="Required semantic evaluation unavailable",
                 kind="semantic", outcome="unavailable", detail="No automatic execution; retry or resolve evidence before reassessment")]
        return dict(verdict=verdict(checks), checks=checks, availability="unavailable", model_calls=int(backend != "offline"),
                    usage={}, provider_latency_ms=None, evaluation=None, request_hash=request_identity(value, policy),
                    error_code="semantic_unavailable")


def evaluate_arm(value, policy, backend):
    """Every comparison arm uses the identical captured state and candidate hard controls."""
    hard = hard_checks(value, policy)
    if backend == "hard_constraints":
        return {"verdict": verdict(hard), "checks": hard, "availability": "intentionally_not_evaluated",
                "model_calls": 0, "usage": {}, "provider_latency_ms": None,
                "note": "Experimental ablation; not an operational policy"}
    if backend == "general_llm_unavailable":
        checks = hard if verdict(hard) == "deny" else hard + [dict(id="classifier_available", kind="semantic",
                  outcome="unavailable", label="General LLM backend unavailable", detail="No model attempt; operational hold only")]
        return {"verdict": verdict(checks), "checks": checks, "availability": "not_required" if verdict(hard) == "deny" else "unavailable",
                "model_calls": 0, "usage": {}, "provider_latency_ms": None, "error_code": "general_llm_backend_not_configured"}
    if backend == "jev_fixture":
        return evaluate_input(value, policy)
    raise ValueError("Comparison backend unavailable")
