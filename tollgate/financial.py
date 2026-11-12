"""Pure financial controls shared by the local gateway and canonical evaluator."""
from datetime import datetime
from tollgate.authorization import ControlCheck, cents, now

def check_constraints(session, proposal, spent_cents: int, reserved_cents: int, exclude=None, evaluation_time=None):
    checks: list[ControlCheck] = []
    def add(cid: str, label: str, good: bool, detail: str, kind: str = "constraint"):
        checks.append(ControlCheck(id=cid, label=label, kind=kind, outcome="pass" if good else "fail", detail=detail))
    mandate, args = session.mandate, proposal.args
    add("mandate_active", "Authorization is active", not mandate.revoked and (evaluation_time or now()) < mandate.expires_at,
        "Revoked" if mandate.revoked else f"Expires {mandate.expires_at.isoformat()}", "authority")
    add("tool_scope", "Tool is within delegated authority", proposal.tool in mandate.permitted_tools,
        f"Permitted tools: {', '.join(mandate.permitted_tools)}", "authority")
    if any(c.outcome == "fail" for c in checks):
        return checks
    required = {"read_invoice": {"invoice_id"}, "lookup_vendor": {"vendor_id"},
                "schedule_payment": {"vendor_id", "amount_usd", "date", "memo"}}[proposal.tool]
    shape_ok = set(args) == required and all(isinstance(args[k], str) and args[k] for k in required - {"amount_usd"})
    add("argument_schema", "Tool arguments match the contract", shape_ok, "Unexpected, missing or invalid arguments are rejected")
    if not shape_ok:
        return checks
    if proposal.tool == "read_invoice":
        add("invoice_scope", "Invoice is authorized", args["invoice_id"] in mandate.invoice_ids, str(args["invoice_id"]))
        return checks
    add("beneficiary_scope", "Beneficiary is authorized", args["vendor_id"] in mandate.beneficiaries, args["vendor_id"])
    if proposal.tool == "lookup_vendor":
        return checks
    invoice_id = args["memo"]
    approved = mandate.approved_invoices.get(invoice_id)
    add("invoice_scope", "Payment is tied to an authorized invoice", approved is not None, invoice_id)
    try:
        amount = cents(args["amount_usd"])
    except ValueError as exc:
        add("amount_valid", "Payment amount is valid", False, str(exc))
        return checks
    try:
        payment_date = datetime.strptime(args["date"], "%Y-%m-%d").date()
        date_ok = payment_date >= (evaluation_time or now()).date()
    except ValueError:
        date_ok = False
    add("payment_date", "Payment date is valid", date_ok, "Use an ISO date today or later")
    if approved is not None:
        add("invoice_match", "Amount and payee match the approved invoice",
            amount == approved["amount_cents"] and args["vendor_id"] == approved["vendor_id"],
            f"Approved ${approved['amount_cents']/100:,.2f} to {approved['vendor_id']}; proposed ${amount/100:,.2f}")
    current = session.vendors.get(args["vendor_id"], {})
    pinned = mandate.beneficiaries.get(args["vendor_id"], {})
    add("destination_pinned", "Payment destination matches the authorized record",
        bool(pinned) and all(current.get(k) == v for k, v in pinned.items()),
        "Bank account and routing number must match the operator-authorized beneficiary")
    duplicate = any(p["invoice_id"] == invoice_id for p in session.ledger)
    pending = any(e.event_id != exclude and e.execution == "pending" and e.tool == "schedule_payment"
                  and e.args.get("memo") == invoice_id for e in session.events)
    add("duplicate_payment", "Invoice has no completed or pending payment", not duplicate and not pending,
        "Each invoice can have one payment; a new request ID does not reset this check")
    remaining = mandate.budget_cents - spent_cents - reserved_cents
    add("cumulative_budget", "Payment fits the remaining task budget", amount <= remaining,
        f"${remaining/100:,.2f} available after completed and reserved payments; proposed ${amount/100:,.2f}")
    if amount > mandate.auto_limit_cents:
        checks.append(ControlCheck(id="human_limit", label="Payment requires operator approval", kind="constraint",
                                   outcome="review", detail=f"${amount/100:,.2f} exceeds the ${mandate.auto_limit_cents/100:,.2f} automatic limit"))
    return checks
