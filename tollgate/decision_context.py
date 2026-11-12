"""Versioned server-built inputs shared by decisions and execution-free evaluation."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")]


class Payment(StrictModel):
    tool: Literal["schedule_payment"] = "schedule_payment"
    invoice_id: Identifier
    amount_minor: Annotated[StrictInt, Field(gt=0, le=1000000000)]
    currency: Literal["USD"] = "USD"
    beneficiary_ref: Identifier
    scheduled_date: date


class InvoiceRead(StrictModel):
    tool: Literal["read_invoice"]
    invoice_id: Identifier


class VendorRead(StrictModel):
    tool: Literal["lookup_vendor"]
    vendor_id: Identifier


ActionV1 = Annotated[Payment | InvoiceRead | VendorRead, Field(discriminator="tool")]


class Claim(StrictModel):
    claim_id: Identifier
    role: Literal["delivery_confirmation", "invoice_link", "exception_resolved"]
    statement: Annotated[str, Field(min_length=1, max_length=1000)]
    invoice_id: Identifier
    source_ids: Annotated[tuple[Identifier, ...], Field(max_length=12)] = ()


class ProposalV1(StrictModel):
    request_id: Identifier
    action: ActionV1
    claims: Annotated[tuple[Claim, ...], Field(max_length=3)] = ()


class DecisionInputV1(StrictModel):
    schema_version: Literal[1] = 1
    workspace_id: str
    mandate_id: str
    proposal_id: str
    run_id: str | None = None
    company_namespace: str
    action: ActionV1
    claims: tuple[Claim, ...] = ()
    evaluation_time: datetime
    # Immutable canonical JSON strings prevent nested in-place mutation as well.
    authority_json: str
    sources_json: str
    financial_json: str
    evidence_json: str
    policy_json: str
    mandate_revision: int
    evidence_revision: int
    policy_hash: str
    normalization_version: str = "minor-usd-1"
    semantic_contract: str = "finance-1"
    compiler_version: str = "studio-1"
    retrieval_version: str = "invoice-scoped-1"

    @property
    def input_hash(self):
        return identity(self)

    def unpack(self, name):
        return json.loads(getattr(self, name + "_json"))


class ExecutorOutcome(StrictModel):
    state: Literal["confirmed", "failed", "unknown", "submitted"]
    operation_id: str | None = None
    receipt: dict | None = None
    error_code: str | None = None
