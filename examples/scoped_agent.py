"""Operator issues a mandate-scoped credential; configure it through secret management.

Run: python -m examples.scoped_agent
A caller-owned stable run key is mandatory; never generate a new key after a timeout.
"""
import os
from datetime import date
from tollgate.client import TollgateClient
from tollgate.decision_context import Payment


def main():
    with TollgateClient(os.environ["TOLLGATE_WORKSPACE_ID"], os.environ["TOLLGATE_MANDATE_ID"],
                        os.environ["TOLLGATE_AGENT_TOKEN"], os.environ.get("TOLLGATE_BASE_URL", "http://127.0.0.1:8765")) as client:
        run_key = os.environ["TOLLGATE_RUN_KEY"]
        for invoice in client.invoices():
            proposal = client.submit(run_key+":"+invoice["id"], Payment(invoice_id=invoice["id"],
                amount_minor=invoice["amount_minor"], beneficiary_ref=invoice["beneficiary_ref"], scheduled_date=date.today()))
            result = client.wait(proposal.id)
            print(invoice["id"], result.state, result.reason_codes)
            # Continue past exceptions; reconnect with get()/wait() using the saved proposal ID.
            if result.receipt_id:
                print(client.receipt(result.receipt_id)["payload"]["operation_id"])


if __name__ == "__main__":
    main()
