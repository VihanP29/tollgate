"""In-memory mock world for Northwind Supply. Call reset() before every run."""
import copy
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_VENDORS0 = json.loads((FIXTURES / "vendors.json").read_text())
_INVOICES0 = json.loads((FIXTURES / "invoices.json").read_text())

VENDORS: dict[str, dict] = {}
INVOICES: dict[str, dict] = {}
LEDGER: list[dict] = []
OUTBOX: list[dict] = []


def reset() -> None:
    VENDORS.clear(); VENDORS.update(copy.deepcopy(_VENDORS0))
    INVOICES.clear(); INVOICES.update(copy.deepcopy(_INVOICES0))
    LEDGER.clear()
    OUTBOX.clear()


def snapshot() -> dict:
    return {"vendors": copy.deepcopy(VENDORS), "ledger": list(LEDGER), "outbox": list(OUTBOX)}


def diff(before: dict) -> dict:
    changed = {}
    for vid, rec in VENDORS.items():
        old = before["vendors"].get(vid, {})
        fields = {k: {"old": old.get(k), "new": v} for k, v in rec.items() if old.get(k) != v}
        if fields:
            changed[vid] = fields
    return {"ledger_added": LEDGER[len(before["ledger"]):],
            "outbox_added": OUTBOX[len(before["outbox"]):],
            "vendors_changed": changed}


reset()
