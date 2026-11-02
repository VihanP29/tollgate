"""Gates the agent can run behind: pass-through (no guard) and HTTP (a running Tollgate API)."""
import time

import httpx

from tollgate.types import Action, DecisionResult


class PassThroughGate:
    name = "passthrough"

    def decide(self, action: Action) -> DecisionResult:
        return DecisionResult(action_id=action.action_id, verdict="allow", risk=0.0, reasons=["no guard"],
                              latency_ms=0.0, cost_usd=0.0, system=self.name)


class HttpGate:
    """Calls POST /check on a Tollgate server, so decisions show up in the dashboard."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout_s: float = 30.0):
        self.name = f"http:{base_url}"
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s)

    def decide(self, action: Action) -> DecisionResult:
        t0 = time.perf_counter()
        resp = self._http.post("/check", json=action.model_dump(mode="json"))
        resp.raise_for_status()
        result = DecisionResult.model_validate(resp.json())
        return result.model_copy(update={"latency_ms": (time.perf_counter() - t0) * 1000})
