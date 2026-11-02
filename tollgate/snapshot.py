"""Build the state object sent with every check."""
from tollgate.types import Action

MAX_DOC_CHARS = 4000


def build_state(action: Action) -> dict:
    state = {
        "proposed_action": {"tool": action.tool, "args": action.args},
        "operator_request": action.operator_request,
        "recent_steps": action.recent_steps[-5:],
        "documents": [d[:MAX_DOC_CHARS] for d in action.documents],
        "company": "Northwind Supply (internal domain: northwind-supply.com)",
    }
    if action.amount_usd is not None:
        state["amount_usd"] = round(action.amount_usd, 2)
    if action.vendor_context is not None:
        state["vendor_context"] = action.vendor_context.model_dump()
    return state


def action_value_usd(action: Action) -> float | None:
    if action.amount_usd is not None:
        return action.amount_usd
    amt = action.args.get("amount_usd")
    return float(amt) if amt is not None else None


BANK_FIELDS = {"bank_account", "routing_number", "iban", "swift", "bank_name", "account_number", "bank_details"}


def touches_bank_details(action: Action) -> bool:
    if action.tool != "update_vendor":
        return False
    fields = action.args.get("fields") or {}
    return any(k.lower() in BANK_FIELDS for k in fields)
