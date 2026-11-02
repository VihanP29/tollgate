from types import SimpleNamespace as NS

import pytest

from tollgate.clients.llm_judge import LLMJudgeGate, load_judge_config, parse_judgement
from tollgate.engine import demo_actions


class FakeMessages:
    def __init__(self, text=None, stop_reason="end_turn", usage=(1000, 200)):
        self.text, self.stop_reason, self.usage, self.calls = text, stop_reason, usage, []

    def create(self, **kw):
        self.calls.append(kw)
        content = [NS(type="thinking", thinking="")]
        if self.text is not None:
            content.append(NS(type="text", text=self.text))
        return NS(content=content, stop_reason=self.stop_reason,
                  usage=NS(input_tokens=self.usage[0], output_tokens=self.usage[1]))


def gate(**kw):
    msgs = FakeMessages(**kw)
    return LLMJudgeGate(client=NS(messages=msgs)), msgs


ACTION = demo_actions()[0]


def test_config_pinned():
    cfg = load_judge_config()
    assert cfg["model"] == "claude-opus-5-5"
    assert cfg["pricing"] == {"usd_per_million_input_tokens": 4.0, "usd_per_million_output_tokens": 20.0}


def test_valid_json():
    g, msgs = gate(text='{"verdict": "deny", "risk": 0.93, "reason": "bank change from invoice"}')
    r = g.decide(ACTION)
    assert (r.verdict, r.risk, r.reasons, r.system) == ("deny", 0.93, ["bank change from invoice"], "llm_judge")
    assert r.cost_usd == pytest.approx(1000 * 4e-6 + 200 * 20e-6)
    assert r.latency_ms >= 0 and g.parse_errors == 0
    req = msgs.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in req


def test_fenced_json_tolerated():
    g, _ = gate(text='```json\n{"verdict": "allow", "risk": 0.05, "reason": "routine"}\n```')
    assert g.decide(ACTION).verdict == "allow"


def test_invalid_json_escalates_and_counts():
    g, _ = gate(text="I think this is fine.")
    r = g.decide(ACTION)
    assert r.verdict == "escalate" and r.reasons[0] == "judge_parse_error" and g.parse_errors == 1
    g.decide(ACTION)
    assert g.parse_errors == 2


def test_bad_verdict_value_is_parse_error():
    g, _ = gate(text='{"verdict": "block", "risk": 0.9, "reason": "x"}')
    assert g.decide(ACTION).reasons[0] == "judge_parse_error"


def test_refusal_escalates():
    g, _ = gate(text='{"verdict": "allow", "risk": 0.0, "reason": "x"}', stop_reason="refusal")
    r = g.decide(ACTION)
    assert r.verdict == "escalate" and r.reasons == ["judge_parse_error", "stop_reason=refusal"]
    assert g.parse_errors == 1


def test_parse_clamps_risk():
    assert parse_judgement('{"verdict": "deny", "risk": 1.7, "reason": ""}') == ("deny", 1.0, "")
    assert parse_judgement('{"verdict": "deny", "risk": "high", "reason": ""}') is None
    assert parse_judgement("") is None
