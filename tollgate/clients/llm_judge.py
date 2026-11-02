"""LLM-judge baseline: one Claude call per action, returns {verdict, risk, reason}.

Model, prompt, effort and prices come from evals/config.yaml. The Anthropic client is injectable
so tests run against a fake. Invalid JSON or a refusal -> escalate with reason "judge_parse_error".
"""
import json
import re
import time
from pathlib import Path

import yaml

from tollgate.snapshot import build_state
from tollgate.types import Action, DecisionResult

CONFIG_FILE = Path(__file__).resolve().parents[2] / "evals" / "config.yaml"
PARSE_ERROR = "judge_parse_error"
VERDICTS = ("allow", "escalate", "deny")

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "risk": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "risk", "reason"],
    "additionalProperties": False,
}


def load_judge_config(path: str | Path = CONFIG_FILE) -> dict:
    return yaml.safe_load(Path(path).read_text())["llm_judge"]


def parse_judgement(text: str) -> tuple[str, float, str] | None:
    """Parse the judge's JSON. Returns None if it is not a valid judgement."""
    text = (text or "").strip()
    candidates = [text]
    m = re.search(r"\{.*\}", text, re.S)  # tolerate code fences or stray prose
    if m and m.group(0) != text:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        verdict = str(obj.get("verdict", "")).strip().lower()
        try:
            risk = float(obj.get("risk"))
        except (TypeError, ValueError):
            continue
        if verdict not in VERDICTS or risk != risk:  # NaN check
            continue
        return verdict, min(1.0, max(0.0, risk)), str(obj.get("reason", "")).strip()
    return None


class LLMJudgeGate:
    name = "llm_judge"

    def __init__(self, client=None, config: dict | None = None):
        self.config = config or load_judge_config()
        self.model = self.config["model"]
        if client is None:
            import anthropic
            client = anthropic.Anthropic(timeout=float(self.config.get("timeout_s", 60)))
        self.client = client
        self.parse_errors = 0
        prices = self.config["pricing"]
        self._in_rate = prices["usd_per_million_input_tokens"] / 1e6
        self._out_rate = prices["usd_per_million_output_tokens"] / 1e6

    def request(self, action: Action) -> dict:
        user = "Proposed action and context:\n" + json.dumps(build_state(action), indent=2, default=str)
        return dict(
            model=self.model,
            max_tokens=int(self.config.get("max_tokens", 2048)),
            system=self.config["system_prompt"],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.config["effort"],
                           "format": {"type": "json_schema", "schema": JSON_SCHEMA}},
        )

    def decide(self, action: Action) -> DecisionResult:
        t0 = time.perf_counter()
        resp = self.client.messages.create(**self.request(action))
        latency_ms = (time.perf_counter() - t0) * 1000
        usage = getattr(resp, "usage", None)
        cost = 0.0
        if usage is not None:
            cost = (getattr(usage, "input_tokens", 0) or 0) * self._in_rate + \
                   (getattr(usage, "output_tokens", 0) or 0) * self._out_rate

        parsed = None
        stop = getattr(resp, "stop_reason", None)
        if stop != "refusal":
            text = "".join(b.text for b in (resp.content or []) if getattr(b, "type", None) == "text")
            parsed = parse_judgement(text)
        if parsed is None:
            self.parse_errors += 1
            verdict, risk = "escalate", 0.5
            reasons = [PARSE_ERROR] + ([f"stop_reason={stop}"] if stop == "refusal" else [])
        else:
            verdict, risk, reason = parsed
            reasons = [reason or f"judge verdict {verdict}"]
        return DecisionResult(action_id=action.action_id, verdict=verdict, risk=round(risk, 4), reasons=reasons,
                              latency_ms=latency_ms, cost_usd=cost, system=self.name)


if __name__ == "__main__":
    import sys
    if "--live" not in sys.argv:
        print("LLMJudgeGate makes live Anthropic calls; pass --live to run it on the demo actions.")
        sys.exit(0)
    from tollgate.engine import demo_actions
    g = LLMJudgeGate()
    for a in demo_actions():
        r = g.decide(a)
        print(f"{a.action_id:10s} -> {r.verdict:8s} risk={r.risk:.2f} ${r.cost_usd:.4f} {r.reasons[0]}")
