from typing import Literal, Protocol
from pydantic import BaseModel, Field

ToolName = Literal["read_invoice", "lookup_vendor", "schedule_payment", "update_vendor", "send_email"]
Verdict = Literal["allow", "escalate", "deny"]


class VendorContext(BaseModel):
    vendor_id: str
    name: str
    record_age_days: int
    last_bank_change_days_ago: int | None = None   # None = never changed
    typical_payment_usd: float | None = None
    bank_change_verified: bool = False            # a verification ticket exists


class Action(BaseModel):
    """One proposed tool call plus the context needed to judge it."""
    action_id: str
    tool: ToolName
    args: dict
    operator_request: str                 # what the human operator actually asked for
    recent_steps: list[str] = Field(default_factory=list)   # last 3-5 agent steps, plain text
    documents: list[str] = Field(default_factory=list)      # text the agent read this turn (invoices, emails)
    vendor_context: VendorContext | None = None
    amount_usd: float | None = None       # filled for schedule_payment; None otherwise


class CheckResult(BaseModel):
    """One policy question answered by one model call."""
    policy_id: str
    p_yes: float | None = None            # yes/no questions
    choice: str | None = None             # choice questions
    score: int | None = None              # score questions (0-3)
    latency_ms: float
    cost_usd: float


class DecisionResult(BaseModel):
    action_id: str
    verdict: Verdict
    risk: float                           # 0.0-1.0, higher = riskier; used for threshold curves
    reasons: list[str]                    # human-readable, e.g. "payee_bank_change p=0.94 > 0.60"
    checks: list[CheckResult] = Field(default_factory=list)
    latency_ms: float                     # end-to-end for this action
    cost_usd: float                       # sum over checks
    system: str                           # "tollgate", "rules", "llm_judge", "passthrough", ...


class Gate(Protocol):
    name: str
    def decide(self, action: Action) -> DecisionResult: ...


# --- Shapes from SPEC sections 3 and 6 (also cross ownership boundaries) ---

class LabeledAction(BaseModel):
    action: Action
    label: Literal["attack", "benign"]
    subset: Literal["attack", "benign", "hard_benign", "public"]
    attack_class: str | None = None
    expected: Verdict
    rationale: str
    source: str


ATTACK_CLASSES = (
    "payee_swap_injection", "amount_inflation", "duplicate_payment", "threshold_splitting",
    "vendor_impersonation", "urgency_pressure", "data_exfiltration", "multi_step_chain",
)


class DecisionRecord(BaseModel):
    decision_id: int
    created_at: str          # ISO 8601
    action: Action
    result: DecisionResult
    resolution: Literal["approve", "reject"] | None = None


class Thresholds(BaseModel):
    allow_below: float       # risk < allow_below -> allow
    deny_above: float        # risk > deny_above -> deny; between -> escalate
