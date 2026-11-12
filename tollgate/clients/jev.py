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
        """Validate the pinned text contract; latency is recorded once per request."""
        from tollgate.decision_context import identity
        started = time.perf_counter()
        requested = model or self.model
        body = {'model': requested, 'state': state, 'questions': compile_questions(policies)}
        try:
            response = self._http.post('/v1/systemone', json=body)
            response.raise_for_status()
            data = response.json()
        except Exception:
            raise JevError('Jev transport or JSON unavailable') from None
        validate_response(data, policies, requested)
        data['requested_model'] = requested
        data['resolved_model'] = data['model']
        data['request_hash'] = identity({'request': body, 'compiler': 'studio-1', 'assets': []})
        data['latency_ms'] = round((time.perf_counter()-started)*1000, 2)
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
            raise JevError(f"Jev HTTP {resp.status_code}")
        data = resp.json()
        input_tokens = data.get("usage", {}).get("input_tokens", 0)
        cost_each = input_tokens * self.usd_per_mtok / 1e6 / len(policies)
        return [parse_answer(p, data["answers"][p.id], latency_ms, cost_each) for p in policies]


def validate_response(data, policies, requested_model):
    import math
    if not isinstance(data, dict) or data.get('model') != requested_model:
        raise JevError('Unexpected response object or resolved model')
    answers = data.get('answers')
    if not isinstance(answers, dict) or set(answers) != {p.id for p in policies}:
        raise JevError('Missing or unexpected answers')
    usage = data.get('usage')
    if not isinstance(usage, dict) or not {'input_tokens', 'output_tokens'} <= set(usage):
        raise JevError('Missing token usage')
    if any(type(v) is not int or v < 0 for v in usage.values()):
        raise JevError('Invalid token usage')
    def number(value, lo, hi):
        if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
            raise JevError('Invalid typed number')
    for policy in policies:
        answer = answers[policy.id]
        expected = 'noul' if policy.type == 'yes_no' else policy.type
        if not isinstance(answer, dict) or answer.get('type') != expected:
            raise JevError('Unexpected answer type')
        if expected == 'noul':
            number(answer.get('noul'), 0, 1)
            continue
        number(answer.get('confidence'), 0, 1)
        probabilities = answer.get('probabilities')
        keys = set(policy.criteria) if expected == 'choice' else {str(i) for i in range(len(policy.criteria))}
        if not isinstance(probabilities, dict) or set(probabilities) != keys:
            raise JevError('Invalid distribution labels')
        for value in probabilities.values():
            number(value, 0, 1)
        # Provider distributions may be rounded: 0.02 absolute tolerance.
        if abs(sum(probabilities.values()) - 1) > 0.02:
            raise JevError('Invalid distribution sum')
        if expected == 'choice':
            selected = answer.get('choice')
            if not isinstance(selected, str) or selected not in keys:
                raise JevError('Unknown choice')
            if probabilities[selected] + 0.02 < max(probabilities.values()):
                raise JevError('Choice disagrees with distribution')
        else:
            number(answer.get('score'), 0, len(policy.criteria) - 1)
            mean = sum(int(k) * v for k, v in probabilities.items())
            if abs(mean - answer['score']) > 0.02 * len(policy.criteria):
                raise JevError('Score disagrees with distribution')
            legend = answer.get('legend')
            # Older provider responses omitted legend; if supplied it must match exactly.
            if legend is not None and legend != {str(i): text for i, text in enumerate(policy.criteria)}:
                raise JevError('Score legend disagrees with criteria')


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

