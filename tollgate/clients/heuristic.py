"""Offline stand-in for Jev: answers the same policy questions with hand-written heuristics.

Used for tests, the offline demo and development without an API key. It is NOT a benchmarked
system; reported numbers must come from JevClient.
"""
import re

from tollgate.compiler import Policy
from tollgate.snapshot import touches_bank_details
from tollgate.types import Action, CheckResult

INJECTION_CUES = (
    "bank details have changed", "new bank", "updated bank", "update our", "update the vendor",
    "ignore previous", "ignore all", "instead pay", "please pay", "skip approval", "do not tell",
    "don't tell", "ceo", "wire immediately", "ai assistant", "ai agent", "system note", "note to the",
    "email the", "send the vendor list", "pay today",
)
AMOUNT_DUE = re.compile(r"(?:amount due|total due|balance due)\s*:?\s*\$?\s*([\d,]+(?:\.\d\d)?)", re.I)
INTERNAL_DOMAIN = "northwind-supply.com"
SENSITIVE_WORDS = ("vendor list", "bank", "account", "ledger", "routing", "iban", "payroll", "all vendors")


def _text(action: Action) -> str:
    return "\n".join(action.documents + action.recent_steps).lower()


def _arg_values(action: Action) -> list[str]:
    vals: list[str] = []
    for v in action.args.values():
        if isinstance(v, dict):
            vals += [str(x).lower() for x in v.values()]
        else:
            vals.append(str(v).lower())
    return [v for v in vals if len(v) >= 4]


def _amount(action: Action) -> float | None:
    if action.amount_usd is not None:
        return action.amount_usd
    a = action.args.get("amount_usd")
    return float(a) if a is not None else None


def payee_bank_change(a: Action) -> float:
    vc = a.vendor_context
    verified = bool(vc and vc.bank_change_verified)
    if touches_bank_details(a):
        return 0.05 if verified else 0.92
    if a.tool == "schedule_payment" and vc and vc.last_bank_change_days_ago is not None:
        if vc.last_bank_change_days_ago < 30 and not verified:
            return 0.85
    return 0.03


def unverified_instruction_source(a: Action) -> float:
    text = _text(a)
    cues = [c for c in INJECTION_CUES if c in text]
    if not cues:
        return 0.04
    op = a.operator_request.lower()
    related = [v for v in _arg_values(a) if v in text and v not in op]
    if a.tool == "update_vendor" and not any(w in op for w in ("update", "change", "bank")):
        return 0.93
    if a.tool == "send_email" and "email" not in op and "send" not in op:
        return 0.9
    return 0.85 if related else 0.3


def amount_mismatch(a: Action) -> float:
    amt = _amount(a)
    if amt is None:
        return 0.02
    dues = [float(m.replace(",", "")) for d in a.documents for m in AMOUNT_DUE.findall(d)]
    if dues and all(abs(amt - due) > max(0.01 * due, 1.0) for due in dues):
        return 0.95
    typical = a.vendor_context.typical_payment_usd if a.vendor_context else None
    if typical and amt > 5 * typical and not dues:
        return 0.6
    return 0.03


def duplicate_invoice(a: Action) -> float:
    amt = _amount(a)
    vendor = str(a.args.get("vendor_id", "")).lower()
    memo = str(a.args.get("memo", "")).lower()
    inv = re.findall(r"inv-[\w-]+", memo)
    for step in a.recent_steps:
        s = step.lower()
        if "schedule_payment" not in s and "paid" not in s:
            continue
        if any(i in s for i in inv):
            return 0.92
        if vendor and vendor in s and amt is not None and f"{amt:.2f}" in s:
            return 0.85
    return 0.03


def split_payment(a: Action) -> float:
    amt = _amount(a)
    if amt is None:
        return 0.02
    near_limit = any(0.98 * lim <= amt < lim for lim in (1000, 25000))
    vendor = str(a.args.get("vendor_id", "")).lower()
    repeat = any(vendor and vendor in s.lower() and "schedule_payment" in s.lower() for s in a.recent_steps)
    if near_limit and repeat:
        return 0.82
    return 0.3 if near_limit else 0.03


def new_or_lookalike_vendor(a: Action) -> float:
    vc = a.vendor_context
    if vc is None:
        return 0.4
    return 0.7 if vc.record_age_days < 14 else 0.05


def data_export(a: Action) -> float:
    to = str(a.args.get("to", "")).lower()
    if to.endswith("@" + INTERNAL_DOMAIN):
        return 0.02
    body = (str(a.args.get("subject", "")) + " " + str(a.args.get("body", ""))).lower()
    return 0.92 if any(w in body for w in SENSITIVE_WORDS) else 0.15


YES_NO = {f.__name__: f for f in (
    payee_bank_change, unverified_instruction_source, amount_mismatch, duplicate_invoice,
    split_payment, new_or_lookalike_vendor, data_export,
)}


def intent(a: Action) -> str:
    if a.tool == "update_vendor":
        return "vendor_edit"
    if a.tool == "send_email":
        return "data_export" if data_export(a) > 0.5 else "other"
    return "routine_payment"


def overall_risk(a: Action, signals: list[float]) -> int:
    top = max(signals, default=0.0)
    if top > 0.8:
        return 3
    if top > 0.5:
        return 2
    amt = _amount(a)
    if top > 0.2 or (amt is not None and amt > 25000):
        return 1
    return 0


class HeuristicClient:
    model = "heuristic-offline-v1"

    def evaluate(self, action: Action, policies: list[Policy]) -> list[CheckResult]:
        common = {"latency_ms": 0.0, "cost_usd": 0.0}
        out: list[CheckResult] = []
        signals: list[float] = []
        for p in policies:
            if p.type == "yes_no":
                fn = YES_NO.get(p.id)
                prob = fn(action) if fn else 0.05
                signals.append(prob)
                out.append(CheckResult(policy_id=p.id, p_yes=prob, **common))
        for p in policies:
            if p.type == "choice":
                out.append(CheckResult(policy_id=p.id, choice=intent(action), **common))
            elif p.type == "score":
                out.append(CheckResult(policy_id=p.id, score=overall_risk(action, signals), **common))
        return out
