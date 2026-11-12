"""Small scoped REST client. Caller-supplied request IDs survive transport retries."""
from __future__ import annotations
import threading
import time
from typing import Literal
from urllib.parse import quote
import httpx
from pydantic import BaseModel, ConfigDict
from tollgate.decision_context import Payment, ProposalV1


class ProposalStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    request_id: str
    mandate_id: str
    verdict: Literal["allow", "review", "deny"] | None
    state: str
    executed: bool
    reason_codes: list[str]
    receipt_id: str | None = None
    status_url: str


class TollgateClient:
    def __init__(self, workspace_id, mandate_id, agent_token, base_url="http://127.0.0.1:8765", timeout=15, transport=None):
        self.workspace_id, self.mandate_id = workspace_id, mandate_id
        self.prefix = "/api/v1/workspaces/" + quote(workspace_id, safe="")
        self.mandate_path = self.prefix + "/mandates/" + quote(mandate_id, safe="")
        self.http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                 headers={"Authorization": "Bearer " + agent_token})

    def _get(self, path):
        response = self.http.get(path)
        response.raise_for_status()
        return response.json()

    def invoices(self):
        return self._get(self.mandate_path + "/invoices")

    def vendors(self):
        return self._get(self.mandate_path + "/vendors")

    def submit(self, request_id: str, action: Payment):
        body = ProposalV1(request_id=request_id, action=action)
        response = self.http.post(self.mandate_path + "/proposals", json=body.model_dump(mode="json"))
        response.raise_for_status()
        return ProposalStatus.model_validate(response.json())

    def get(self, proposal_id):
        return ProposalStatus.model_validate(self._get(self.prefix + "/proposals/" + quote(proposal_id, safe="")))

    def receipt(self, receipt_id):
        return self._get(self.prefix + "/receipts/" + quote(receipt_id, safe=""))

    def wait(self, proposal_id, timeout=30, interval=0.5, cancel: threading.Event | None = None, stop_on_review=True):
        """Cancelling polling does not revoke authority or cancel accepted execution."""
        if timeout < 0 or interval <= 0:
            raise ValueError("Invalid polling bounds")
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Polling cancelled; accepted work is unchanged")
            result = self.get(proposal_id)
            if result.state in {"confirmed", "denied", "cancelled", "failed", "held", "unknown"} or (
                    stop_on_review and result.state == "awaiting_review"):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Proposal still pending; poll later using the same proposal ID")
            if cancel is not None:
                cancel.wait(min(interval, remaining))
            else:
                time.sleep(min(interval, remaining))

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
