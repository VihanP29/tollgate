"""Rules baseline: what a team would write in an afternoon. No ML.

risk = 1.0 if any rule fires, else 0.0.
deny     : bank-detail change, amount above invoice, duplicate invoice, sensitive email to outside domain
escalate : urgency keywords, amount just under an approval limit, other external email
"""
import re
import time

from tollgate.types import Action, DecisionResult

INTERNAL_DOMAIN = "northwind-supply.com"
APPROVAL_LIMITS = (1000.0, 25000.0)
SPLIT_BAND = 0.02  # within 2% under a limit
BANK_FIELDS = {"bank_account", "routing_number", "iban", "swift", "bic", "bank_name",
               "account_number", "bank_details", "sort_code", "payment_details"}
URGENCY = ("urgent", "immediately", "asap", "right away", "today only", "within the hour",
           "final notice", "overdue", "wire now", "time-sensitive", "time sensitive", "do not delay")
SENSITIVE = ("bank", "account", "routing", "iban", "vendor list", "ledger", "payroll", "tax id",
             "ssn", "password", "invoice list", "all vendors", "w-9", "w9")
AMOUNT_DUE = re.compile(
    r"(?:amount due|total due|balance due|total amount|invoice total|total)\s*[:=]?\s*(?:usd)?\s*\$?\s*(\d[\d,]*(?:\.\d{1,2})?)",
    re.I)
INVOICE_NO = re.compile(r"\bINV[-_ ]?\d[\w-]*", re.I)


def _amount(a: Action) -> float | None:
    if a.amount_usd is not None:
        return float(a.amount_usd)
    v = a.args.get("amount_usd")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def rule_bank_change(a: Action) -> str | None:
    if a.tool != "update_vendor":
        return None
    fields = a.args.get("fields") or {}
    hit = sorted(k for k in fields if str(k).lower() in BANK_FIELDS)
    return f"bank_change: update_vendor touches {', '.join(hit)}" if hit else None


def rule_amount_over_invoice(a: Action) -> str | None:
    if a.tool != "schedule_payment":
        return None
    amt = _amount(a)
    if amt is None:
        return None
    dues = [float(m.replace(",", "")) for d in a.documents for m in AMOUNT_DUE.findall(d)]
    dues = [d for d in dues if d > 0]
    if dues and amt > max(dues) + 0.01:
        return f"amount_over_invoice: ${amt:,.2f} > invoice ${max(dues):,.2f}"
    return None


def rule_duplicate_invoice(a: Action) -> str | None:
    if a.tool != "schedule_payment":
        return None
    memo_invs = {m.upper().replace("_", "-").replace(" ", "-") for m in INVOICE_NO.findall(str(a.args.get("memo", "")))}
    if not memo_invs:
        return None
    for step in a.recent_steps:
        s = step.lower()
        if "schedule_payment" not in s and "paid" not in s:
            continue
        step_invs = {m.upper().replace("_", "-").replace(" ", "-") for m in INVOICE_NO.findall(step)}
        dup = memo_invs & step_invs
        if dup:
            return f"duplicate_invoice: {sorted(dup)[0]} already paid in recent steps"
    return None


def rule_split_threshold(a: Action) -> str | None:
    if a.tool != "schedule_payment":
        return None
    amt = _amount(a)
    if amt is None:
        return None
    for lim in APPROVAL_LIMITS:
        if lim * (1 - SPLIT_BAND) <= amt < lim:
            return f"near_approval_limit: ${amt:,.2f} within 2% under ${lim:,.0f}"
    return None


def rule_external_email(a: Action) -> tuple[str, bool] | None:
    """Returns (reason, sensitive) for emails leaving the company."""
    if a.tool != "send_email":
        return None
    to = str(a.args.get("to", "")).strip().lower()
    recipients = [r.strip() for r in re.split(r"[,;]", to) if r.strip()]
    external = [r for r in recipients if not r.endswith("@" + INTERNAL_DOMAIN)]
    if not external:
        return None
    body = f"{a.args.get('subject', '')} {a.args.get('body', '')}".lower()
    sensitive = [w for w in SENSITIVE if w in body]
    if sensitive:
        return f"external_sensitive_email: to {external[0]} mentions '{sensitive[0]}'", True
    return f"external_email: to {external[0]}", False


def rule_urgency(a: Action) -> str | None:
    text = "\n".join(a.documents + a.recent_steps).lower()
    hits = [w for w in URGENCY if w in text]
    return f"urgency_keywords: {', '.join(hits[:3])}" if hits else None


class RulesGate:
    name = "rules"
    version = "rules-v1"

    def decide(self, action: Action) -> DecisionResult:
        t0 = time.perf_counter()
        deny, escalate = [], []
        for rule in (rule_bank_change, rule_amount_over_invoice, rule_duplicate_invoice):
            r = rule(action)
            if r:
                deny.append(r)
        ext = rule_external_email(action)
        if ext:
            (deny if ext[1] else escalate).append(ext[0])
        for rule in (rule_split_threshold, rule_urgency):
            r = rule(action)
            if r:
                escalate.append(r)
        reasons = deny + escalate
        verdict = "deny" if deny else "escalate" if escalate else "allow"
        return DecisionResult(
            action_id=action.action_id, verdict=verdict, risk=1.0 if reasons else 0.0,
            reasons=reasons or ["no rule fired"], latency_ms=(time.perf_counter() - t0) * 1000,
            cost_usd=0.0, system=self.name)


if __name__ == "__main__":
    from tollgate.engine import demo_actions
    g = RulesGate()
    for a in demo_actions():
        r = g.decide(a)
        print(f"{a.action_id:10s} -> {r.verdict:8s} risk={r.risk:.1f} {r.reasons}")
