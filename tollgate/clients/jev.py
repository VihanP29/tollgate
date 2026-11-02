"""TypeSafe Jev client: POST /v1/systemone with every applicable policy as one question."""
import os
import time

import httpx

from tollgate.compiler import Policy, compile_questions
from tollgate.snapshot import build_state
from tollgate.types import Action, CheckResult

DEFAULT_BASE_URL = "https://api.typesafe.ai"


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(
        self,
        model: str,
        usd_per_million_input_tokens: float,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.usd_per_mtok = usd_per_million_input_tokens
        key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not key and transport is None:
            raise JevError("TYPESAFE_API_KEY is not set")
        self._http = httpx.Client(
            base_url=base_url or os.environ.get("TYPESAFE_BASE_URL", DEFAULT_BASE_URL),
            headers={"Authorization": f"Bearer {key or 'test'}"},
            timeout=timeout_s,
            transport=transport,
        )

    def evaluate_full(self, state, policies, model=None):
        """Preserve typed distributions; legacy evaluate remains compatible."""
        import math
        started = time.perf_counter()
        response = self._http.post('/v1/systemone', json={'model':model or self.model,'state':state,'questions':compile_questions(policies)})
        response.raise_for_status()
        data=response.json()
        if set(data.get('answers',{}))!={p.id for p in policies}: raise JevError('Missing or unexpected answers')
        def number(value,lo,hi):
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi: raise JevError('Invalid typed number')
        for p in policies:
            a=data['answers'][p.id]; expected='noul' if p.type=='yes_no' else p.type
            if a.get('type')!=expected: raise JevError('Unexpected answer type')
            if expected=='noul': number(a.get('noul'),0,1); continue
            number(a.get('confidence'),0,1)
            probs=a.get('probabilities',{})
            keys=set(p.criteria) if expected=='choice' else {str(i) for i in range(len(p.criteria))}
            if set(probs)!=keys: raise JevError('Invalid distribution labels')
            for value in probs.values(): number(value,0,1)
            if abs(sum(probs.values())-1)>0.02: raise JevError('Invalid distribution sum')
            if expected=='choice' and a.get('choice') not in keys: raise JevError('Unknown choice')
            if expected=='score': number(a.get('score'),0,len(p.criteria)-1)
        data['latency_ms']=round((time.perf_counter()-started)*1000,2)
        return data

    def request_body(self, action: Action, policies: list[Policy]) -> dict:
        return {"model": self.model, "state": build_state(action), "questions": compile_questions(policies)}

    def evaluate(self, action: Action, policies: list[Policy]) -> list[CheckResult]:
        if not policies:
            return []
        t0 = time.perf_counter()
        resp = self._http.post("/v1/systemone", json=self.request_body(action, policies))
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            raise JevError(f"Jev {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        input_tokens = data.get("usage", {}).get("input_tokens", 0)
        cost_each = input_tokens * self.usd_per_mtok / 1e6 / len(policies)
        return [parse_answer(p, data["answers"][p.id], latency_ms, cost_each) for p in policies]


def parse_answer(policy: Policy, answer: dict, latency_ms: float, cost_usd: float) -> CheckResult:
    common = {"policy_id": policy.id, "latency_ms": latency_ms, "cost_usd": cost_usd}
    if policy.type == "yes_no":
        return CheckResult(p_yes=float(answer["noul"]), **common)
    if policy.type == "choice":
        return CheckResult(choice=answer["choice"], **common)
    return CheckResult(score=round(float(answer["score"])), **common)


if __name__ == "__main__":
    import json
    import sys

    from tollgate.compiler import load_policies
    from tollgate.engine import demo_actions

    ps = load_policies()
    action = demo_actions()[0]
    if "--live" not in sys.argv:
        print(json.dumps(JevClient(ps.model, 0, transport=httpx.MockTransport(lambda r: None)).request_body(
            action, ps.applicable(action)), indent=2))
        print("\n(dry run; pass --live with TYPESAFE_API_KEY set to call Jev)")
    else:
        for c in JevClient(ps.model, ps.usd_per_million_input_tokens).evaluate(action, ps.applicable(action)):
            print(c)

