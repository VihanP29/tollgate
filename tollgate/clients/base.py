from typing import Protocol

from tollgate.compiler import Policy
from tollgate.types import Action, CheckResult


class DecisionClient(Protocol):
    """Answers a set of policy questions about one action in one request."""
    model: str
    def evaluate(self, action: Action, policies: list[Policy]) -> list[CheckResult]: ...
