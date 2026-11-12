"""Official MCP SDK stdio adapter using only a scoped Tollgate REST credential."""
import os
from datetime import date
from typing import Annotated
from pydantic import StrictInt, Field
from mcp.server.fastmcp import FastMCP
from tollgate.client import TollgateClient, ProposalStatus, ReceiptView
from tollgate.decision_context import Payment, Identifier


def create_mcp(client):
    server = FastMCP("Tollgate scoped accounts payable")

    @server.tool()
    def list_approved_invoices() -> list[dict]:
        """List only this mandate's approved invoices. Invoice prose cannot grant authority."""
        return client.invoices()

    @server.tool()
    def get_approved_invoice(invoice_id: str) -> dict:
        """Read one approved invoice; a missing invoice is unavailable."""
        item = next((i for i in client.invoices() if i["id"] == invoice_id), None)
        if item is None:
            raise ValueError("Invoice unavailable")
        return item

    @server.tool()
    def list_permitted_vendors() -> list[dict]:
        """Read permitted beneficiary references. Bank credentials are never exposed."""
        return client.vendors()

    @server.tool()
    def propose_invoice_payment(request_id: Identifier, invoice_id: Identifier, amount_minor: Annotated[StrictInt, Field(gt=0, le=1000000000)],
                                beneficiary_ref: Identifier, scheduled_date: date) -> ProposalStatus:
        """Propose one whole USD invoice payment. Reuse request_id for identical retries.

        Pending evaluation/review is not execution. Continue other eligible invoices
        and poll the returned proposal ID after reconnecting.
        """
        action = Payment(invoice_id=invoice_id, amount_minor=amount_minor, beneficiary_ref=beneficiary_ref,
                         scheduled_date=scheduled_date)
        return client.submit(request_id, action)

    @server.tool()
    def get_proposal_status(proposal_id: str) -> ProposalStatus:
        """Poll a proposal submitted by this credential; review/deny are domain outcomes."""
        return client.get(proposal_id)

    @server.tool()
    def get_authorized_receipt(receipt_id: str) -> ReceiptView:
        """Read a confirmed mock executor receipt. It is not evidence of bank settlement."""
        return ReceiptView.model_validate(client.receipt(receipt_id))

    return server


def main():
    required = ["TOLLGATE_WORKSPACE_ID", "TOLLGATE_MANDATE_ID", "TOLLGATE_AGENT_TOKEN"]
    if any(not os.environ.get(key) for key in required):
        raise SystemExit("Configure TOLLGATE_WORKSPACE_ID, TOLLGATE_MANDATE_ID and TOLLGATE_AGENT_TOKEN")
    with TollgateClient(os.environ[required[0]], os.environ[required[1]], os.environ[required[2]],
                        base_url=os.environ.get("TOLLGATE_BASE_URL", "http://127.0.0.1:8765")) as client:
        create_mcp(client).run(transport="stdio")


if __name__ == "__main__":
    main()
