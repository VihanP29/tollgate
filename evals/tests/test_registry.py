import pytest

from evals.registry import REGISTRY, GateUnavailable, load_gate


def test_names():
    assert {"passthrough", "rules", "llm_judge", "tollgate", "tollgate_offline", "tollgate_decisions"} <= set(REGISTRY)


@pytest.mark.parametrize("name", ["passthrough", "rules", "tollgate_offline"])
def test_offline_gates_load(name):
    g = load_gate(name)
    assert hasattr(g, "decide")


@pytest.mark.parametrize("name", ["llm_judge", "tollgate", "tollgate_decisions"])
def test_live_gates_refuse_without_flag(name):
    with pytest.raises(GateUnavailable, match="--live"):
        load_gate(name)


def test_tollgate_needs_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(GateUnavailable, match="TYPESAFE_API_KEY"):
        load_gate("tollgate", live=True)


def test_tollgate_decisions_not_built():
    with pytest.raises(GateUnavailable):
        load_gate("tollgate_decisions", live=True)


def test_unknown_gate():
    with pytest.raises(GateUnavailable, match="unknown"):
        load_gate("nope")


def test_offline_label():
    assert "NOT the real system" in REGISTRY["tollgate_offline"].description
    assert load_gate("tollgate_offline").name == "tollgate_offline"
