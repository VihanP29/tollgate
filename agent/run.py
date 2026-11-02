"""Agent loop: the planner proposes tool calls, the gate decides, allowed calls execute. No framework."""
import argparse
import json

from pydantic import BaseModel, Field

from agent import world
from agent.gates import HttpGate, PassThroughGate
from agent.planners import ClaudePlanner, ScriptedPlanner, ToolOutcome
from agent.tools import ToolError, execute
from tollgate.types import Action, DecisionResult, Gate, VendorContext

TOOL_NAMES = {"read_invoice", "lookup_vendor", "schedule_payment", "update_vendor", "send_email"}
RECENT = 5


class Step(BaseModel):
    action: Action
    decision: DecisionResult
    executed: bool
    result: str
    is_error: bool = False


class RunTrace(BaseModel):
    operator_request: str
    gate: str
    planner: str
    steps: list[Step] = Field(default_factory=list)
    review_queue: list[Step] = Field(default_factory=list)
    final_text: str | None = None
    hit_max_steps: bool = False
    diff: dict = Field(default_factory=dict)   # world.diff(): ledger_added, outbox_added, vendors_changed

    @property
    def payments(self) -> list[dict]:
        return self.diff.get("ledger_added", [])


def vendor_context(vendor_id: str | None) -> VendorContext | None:
    rec = world.VENDORS.get(vendor_id or "")
    if rec is None:
        return None
    return VendorContext(**{k: rec.get(k) for k in VendorContext.model_fields if rec.get(k) is not None})


def describe(tool: str, args: dict) -> str:
    if tool == "schedule_payment":
        return f"schedule_payment({args.get('vendor_id')}, {float(args.get('amount_usd', 0)):.2f}, memo={args.get('memo')})"
    if tool == "update_vendor":
        fields = ", ".join(f"{k}={v}" for k, v in (args.get("fields") or {}).items())
        return f"update_vendor({args.get('vendor_id')}, {fields})"
    if tool == "send_email":
        return f"send_email(to={args.get('to')}, subject={args.get('subject')!r})"
    return f"{tool}({', '.join(str(v) for v in args.values())})"


def summarize(tool: str, value) -> str:
    if tool == "read_invoice":
        due = next((l for l in str(value).splitlines() if l.lower().startswith("amount due")), "")
        return f"{str(value).splitlines()[0]} invoice, {due or 'read'}"
    if tool == "lookup_vendor":
        return f"{value.get('name')}, bank {value.get('bank_account')}"
    if tool == "update_vendor":
        return "vendor record updated"
    return str(value)


def run_task(operator_request: str, gate: Gate, max_steps: int = 15, planner=None) -> RunTrace:
    """max_steps caps the number of proposed tool calls in this run."""
    planner = planner or ScriptedPlanner()
    before = world.snapshot()
    trace = RunTrace(operator_request=operator_request, gate=gate.name, planner=planner.name)
    history: list[str] = []
    docs: list[tuple[str | None, str]] = []   # (vendor_id, text) of invoices read this run
    last_payment_doc = 0                       # index into docs at the last executed payment
    planner.start(operator_request)
    outcomes: list[ToolOutcome] = []
    n = 0
    while True:
        calls = planner.next_calls(outcomes)
        if not calls:
            break
        outcomes = []
        for call in calls:
            if n >= max_steps:
                trace.hit_max_steps = True
                break
            n += 1
            args = dict(call.args)
            vendor_id = args.get("vendor_id")
            amount = float(args["amount_usd"]) if call.tool == "schedule_payment" and "amount_usd" in args else None
            if vendor_id:
                documents = [t for v, t in docs if v == vendor_id]
            else:
                documents = [t for _, t in docs[last_payment_doc:]]
            if call.tool not in TOOL_NAMES:
                outcomes.append(ToolOutcome(id=call.id, content=f"unknown tool {call.tool}", is_error=True))
                continue
            action = Action(action_id=f"run-{n:03d}", tool=call.tool, args=args, operator_request=operator_request,
                            recent_steps=history[-RECENT:], documents=documents,
                            vendor_context=vendor_context(vendor_id), amount_usd=amount)
            decision = gate.decide(action)
            executed, is_error = False, False
            if decision.verdict == "allow":
                try:
                    value = execute(call.tool, args)
                    executed = True
                    content = json.dumps(value) if isinstance(value, dict) else str(value)
                    note = summarize(call.tool, value)
                    if call.tool == "read_invoice":
                        inv = world.INVOICES.get(args.get("invoice_id"), {})
                        docs.append((inv.get("vendor_id"), str(value)))
                    if call.tool == "schedule_payment":
                        last_payment_doc = len(docs)
                except (ToolError, TypeError, KeyError) as e:
                    content, note, is_error = f"error: {e}", f"error: {e}", True
            elif decision.verdict == "escalate":
                content = ("PENDING HUMAN REVIEW: Tollgate escalated this action; it was not executed. "
                           f"Reason: {decision.reasons[0] if decision.reasons else 'n/a'}")
                note = "ESCALATED (pending review)"
            else:
                content = "DENIED by Tollgate; the action was not executed. Reasons: " + "; ".join(decision.reasons)
                note, is_error = f"DENIED ({decision.reasons[0] if decision.reasons else 'policy'})", True
            history.append(f"{describe(call.tool, args)} -> {note}")
            step = Step(action=action, decision=decision, executed=executed, result=content, is_error=is_error)
            trace.steps.append(step)
            if decision.verdict == "escalate":
                trace.review_queue.append(step)
            outcomes.append(ToolOutcome(id=call.id, content=content, is_error=is_error))
        if trace.hit_max_steps:
            break
    trace.final_text = getattr(planner, "final_text", None)
    trace.diff = world.diff(before)
    return trace


def make_gate(kind: str, url: str) -> Gate:
    if kind == "http":
        return HttpGate(url)
    if kind == "heuristic":
        from tollgate.engine import build_gate
        return build_gate("heuristic")
    return PassThroughGate()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the AP agent on one operator request.")
    ap.add_argument("request")
    ap.add_argument("--gate", choices=["passthrough", "http", "heuristic"], default="passthrough")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--live", action="store_true", help="use Claude (network, needs ANTHROPIC_API_KEY)")
    ap.add_argument("--max-steps", type=int, default=15)
    a = ap.parse_args()
    world.reset()
    t = run_task(a.request, make_gate(a.gate, a.url), a.max_steps, ClaudePlanner() if a.live else None)
    for s in t.steps:
        print(f"{s.decision.verdict.upper():8s} {describe(s.action.tool, s.action.args):60s} {s.result[:70]!r}")
    print(json.dumps(t.diff, indent=2))
    print(t.final_text or "")
