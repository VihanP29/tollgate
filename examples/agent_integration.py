"""A minimal client for an agent that has only its scoped Tollgate credential.

An operator provisions a session first. Pass its session ID and agent token through
your agent host's secret management; do not place the operator token in the model.
"""
import uuid

import httpx


class PendingReview(Exception):
    pass


class ActionDenied(Exception):
    pass


class TollgateTools:
    def __init__(self, session_id: str, agent_token: str, base_url: str = "http://127.0.0.1:8000"):
        self.session_id = session_id
        self.client = httpx.Client(base_url=base_url, timeout=30,
                                   headers={"Authorization": "Bearer " + agent_token})

    def call(self, tool: str, args: dict, request_id: str | None = None):
        """Supply a stable request_id when retrying an action after a transport failure."""
        response = self.client.post(f"/api/sessions/{self.session_id}/actions", json={
            "request_id": request_id or uuid.uuid4().hex, "tool": tool, "args": args})
        response.raise_for_status()
        event = response.json()
        if event["execution"] == "executed":
            return event["result"]
        if event["execution"] == "pending":
            raise PendingReview("; ".join(event["reasons"]))
        raise ActionDenied("; ".join(event["reasons"]))

    def close(self):
        self.client.close()
