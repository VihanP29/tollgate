"""The five mock tools. No network, no real money."""
from agent import world

TOOL_SCHEMAS = [
    {"name": "read_invoice", "description": "Read an invoice by id and return its text.",
     "input_schema": {"type": "object", "properties": {"invoice_id": {"type": "string"}},
                      "required": ["invoice_id"], "additionalProperties": False}},
    {"name": "lookup_vendor", "description": "Return the vendor record, including bank details.",
     "input_schema": {"type": "object", "properties": {"vendor_id": {"type": "string"}},
                      "required": ["vendor_id"], "additionalProperties": False}},
    {"name": "schedule_payment", "description": "Schedule a payment to a vendor's bank account on file.",
     "input_schema": {"type": "object", "properties": {
         "vendor_id": {"type": "string"}, "amount_usd": {"type": "number"},
         "date": {"type": "string", "description": "ISO 8601 date"}, "memo": {"type": "string"}},
         "required": ["vendor_id", "amount_usd", "date", "memo"], "additionalProperties": False}},
    {"name": "update_vendor", "description": "Update fields on a vendor record (e.g. bank_account, routing_number, email).",
     "input_schema": {"type": "object", "properties": {
         "vendor_id": {"type": "string"}, "fields": {"type": "object"}},
         "required": ["vendor_id", "fields"], "additionalProperties": False}},
    {"name": "send_email", "description": "Send an email.",
     "input_schema": {"type": "object", "properties": {
         "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
         "required": ["to", "subject", "body"], "additionalProperties": False}},
]


class ToolError(Exception):
    pass


def read_invoice(invoice_id: str) -> str:
    inv = world.INVOICES.get(invoice_id)
    if inv is None:
        raise ToolError(f"no invoice {invoice_id}")
    return inv["text"]


def lookup_vendor(vendor_id: str) -> dict:
    rec = world.VENDORS.get(vendor_id)
    if rec is None:
        raise ToolError(f"no vendor {vendor_id}")
    return dict(rec)


def schedule_payment(vendor_id: str, amount_usd: float, date: str, memo: str) -> str:
    rec = lookup_vendor(vendor_id)
    payment_id = f"PAY-{len(world.LEDGER) + 1:04d}"
    world.LEDGER.append({"payment_id": payment_id, "vendor_id": vendor_id, "amount_usd": round(float(amount_usd), 2),
                         "date": date, "memo": memo, "bank_account": rec["bank_account"]})
    return payment_id


def update_vendor(vendor_id: str, fields: dict) -> dict:
    if vendor_id not in world.VENDORS:
        raise ToolError(f"no vendor {vendor_id}")
    world.VENDORS[vendor_id].update(fields)
    if {"bank_account", "routing_number"} & set(fields):
        world.VENDORS[vendor_id]["last_bank_change_days_ago"] = 0
        world.VENDORS[vendor_id]["bank_change_verified"] = False
    return dict(world.VENDORS[vendor_id])


def send_email(to: str, subject: str, body: str) -> str:
    message_id = f"MSG-{len(world.OUTBOX) + 1:04d}"
    world.OUTBOX.append({"message_id": message_id, "to": to, "subject": subject, "body": body})
    return message_id


TOOLS = {f.__name__: f for f in (read_invoice, lookup_vendor, schedule_payment, update_vendor, send_email)}


def execute(tool: str, args: dict):
    return TOOLS[tool](**args)
