"""Normalized executor contract; financial execution is reachable only by outbox."""
from typing import Protocol
from tollgate.decision_context import ExecutorOutcome, Payment, identity


class Executor(Protocol):
    safe_retry: bool
    def dispatch(self, provider_key: str, action: Payment) -> ExecutorOutcome: ...
    def lookup(self, provider_key: str, action: Payment) -> ExecutorOutcome: ...


class MockExecutor:
    """The only effect is the transactional Tollgate mock receipt, never a remote write."""
    safe_retry = True

    def dispatch(self, provider_key, action):
        operation_id = "mock_" + identity(provider_key)[:24]
        return ExecutorOutcome(state="confirmed", operation_id=operation_id, receipt={
            "schema_version": 1, "kind": "mock_payment", "settlement": "not_applicable",
            "provider_key": provider_key, "operation_id": operation_id, "invoice_id": action.invoice_id,
            "amount_minor": action.amount_minor, "currency": "USD", "beneficiary_ref": action.beneficiary_ref,
            "scheduled_date": action.scheduled_date.isoformat(), "sandbox": True})

    def lookup(self, provider_key, action):
        return ExecutorOutcome(state="unknown", error_code="mock_result_not_persisted")
