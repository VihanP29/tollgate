"""Load the policy YAML and compile applicable policies into Jev questions."""
import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from tollgate.types import Action

DEFAULT_POLICY_FILE = Path(__file__).resolve().parent.parent / "policies" / "finance.yaml"


class Policy(BaseModel):
    id: str
    type: Literal["yes_no", "choice", "score"]
    tools: list[str]
    instructions: str
    critical: bool = False
    criteria: dict[str, str] | list[str] | None = None


class Tier(BaseModel):
    allow_below: float
    deny_above: float
    max_usd: float | None = None
    double_check: bool = False


class PolicySet(BaseModel):
    version: int
    model: str
    usd_per_million_input_tokens: float
    tiers: dict[str, Tier]
    policies: list[Policy]
    file_hash: str

    def applicable(self, action: Action) -> list[Policy]:
        return [p for p in self.policies if action.tool in p.tools]

    def get(self, policy_id: str) -> Policy:
        return next(p for p in self.policies if p.id == policy_id)


def load_policies(path: str | Path = DEFAULT_POLICY_FILE) -> PolicySet:
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw)
    return PolicySet(
        version=data["version"],
        model=data["model"],
        usd_per_million_input_tokens=data["pricing"]["usd_per_million_input_tokens"],
        tiers={k: Tier(**v) for k, v in data["tiers"].items()},
        policies=[Policy(**p) for p in data["policies"]],
        file_hash=hashlib.sha256(raw).hexdigest()[:12],
    )


def to_question(policy: Policy) -> dict:
    """One policy -> one TypeSafe System One question (types: noul, choice, score)."""
    if policy.type == "yes_no":
        q = {"type": "noul", "instructions": policy.instructions}
        if policy.criteria:
            q["criteria"] = policy.criteria
        return q
    return {"type": policy.type, "instructions": policy.instructions, "criteria": policy.criteria}


def compile_questions(policies: list[Policy]) -> dict[str, dict]:
    return {p.id: to_question(p) for p in policies}


if __name__ == "__main__":
    import json
    ps = load_policies()
    print(f"{len(ps.policies)} policies, hash {ps.file_hash}")
    print(json.dumps(compile_questions(ps.policies[:2]), indent=2))
