"""Gates under test, loaded by name. Missing or unconfigured gates raise GateUnavailable (never crash)."""
import os
from dataclasses import dataclass
from typing import Callable

from tollgate.types import Gate


class GateUnavailable(Exception):
    pass


@dataclass
class GateSpec:
    name: str
    description: str
    live: bool                       # makes paid/network calls; needs --live
    factory: Callable[[], Gate]


def _passthrough() -> Gate:
    from agent.gates import PassThroughGate
    return PassThroughGate()


def _rules() -> Gate:
    from tollgate.baselines.rules import RulesGate
    return RulesGate()


def _llm_judge() -> Gate:
    from tollgate.clients.llm_judge import LLMJudgeGate
    # No env-var check: the SDK can also use an `ant auth login` profile. Refuse only if construction fails.
    try:
        return LLMJudgeGate()
    except Exception as e:  # missing credentials etc.
        raise GateUnavailable(f"llm_judge: could not create Anthropic client ({e})") from e


def _tollgate() -> Gate:
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise GateUnavailable("tollgate: TYPESAFE_API_KEY is not set")
    from tollgate.engine import build_gate
    return build_gate("jev")


def _tollgate_offline() -> Gate:
    from tollgate.engine import build_gate
    return build_gate("heuristic")


def _tollgate_decisions() -> Gate:
    try:
        from tollgate.clients import openai_decisions  # noqa: F401  (Vihan's, not built yet)
    except ImportError as e:
        raise GateUnavailable("tollgate_decisions: tollgate/clients/openai_decisions.py not built yet") from e
    from tollgate.engine import build_gate
    try:
        return build_gate("openai_decisions")
    except Exception as e:
        raise GateUnavailable(f"tollgate_decisions: engine has no openai_decisions backend yet ({e})") from e


REGISTRY: dict[str, GateSpec] = {s.name: s for s in (
    GateSpec("passthrough", "no guard; always allow", False, _passthrough),
    GateSpec("rules", "afternoon rules baseline", False, _rules),
    GateSpec("llm_judge", "one Claude call per action", True, _llm_judge),
    GateSpec("tollgate", "Tollgate engine + live Jev", True, _tollgate),
    GateSpec("tollgate_offline", "Tollgate engine + OFFLINE heuristic stand-in (NOT the real system)", False,
             _tollgate_offline),
    GateSpec("tollgate_decisions", "Tollgate engine + OpenAI Decisions", True, _tollgate_decisions),
)}


def model_versions(gate: Gate) -> dict:
    """Best-effort version strings for the sidecar."""
    out: dict = {"system": getattr(gate, "name", None)}
    client = getattr(gate, "client", None)
    if getattr(gate, "model", None):
        out["model"] = gate.model
    elif client is not None and getattr(client, "model", None):
        out["model"] = client.model
    if getattr(gate, "version", None):
        out["version"] = gate.version
    cfg = getattr(gate, "config", None)
    if isinstance(cfg, dict) and "effort" in cfg:
        out["effort"] = cfg["effort"]
    return out


def load_gate(name: str, live: bool = False) -> Gate:
    spec = REGISTRY.get(name)
    if spec is None:
        raise GateUnavailable(f"{name}: unknown gate (known: {', '.join(REGISTRY)})")
    if spec.live and not live:
        raise GateUnavailable(f"{name}: makes live API calls; re-run with --live")
    return spec.factory()
