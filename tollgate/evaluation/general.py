"""Usage-metered general judge capability. No executor or financial mutation API."""
from tollgate.clients.general_judge import GeneralJudgeError
from tollgate.decision_context import Payment, identity
from tollgate.services.platform import Conflict, QuotaExceeded


def unavailable(hard,code,model_calls=0,usage=None,latency=None):
    from tollgate.evaluation.evaluator import verdict
    checks=hard+[dict(id="general_semantic_available",kind="semantic",outcome="unavailable",
        label="General semantic judge unavailable",detail=code)]
    return dict(verdict=verdict(checks),checks=checks,availability=code if code in {"timeout","rate_limited","refusal","malformed_output","quota_exhausted","truncated_output"} else "unavailable",error_code=code,
                model_calls=model_calls,usage=usage or {},provider_latency_ms=latency,evaluation=None)


class BudgetedGeneralJudge:
    def __init__(self,client,usage):
        self.client,self.usage=client,usage
        self.config=client.config

    @staticmethod
    def _cached(record,hard):
        if record["state"]=="overrun":
            result=unavailable(hard,"usage_allowance_exceeded")
        else:
            from tollgate.evaluation.evaluator import verdict
            semantic=[check for check in record["response"]["checks"] if check["kind"]=="semantic"]
            result={**record["response"],"checks":hard+semantic,"verdict":verdict(hard+semantic),
                    "model_calls":0,"usage":{},"provider_latency_ms":None}
        return {**result,"reused_answers":True,"usage_request_id":record["id"]}

    def evaluate(self,value,policy,*,logical_key,attempt,resource_kind,resource_id,resume_from_history=False):
        from tollgate.evaluation.evaluator import hard_checks,verdict
        hard=hard_checks(value,policy)
        if verdict(hard)=="deny" or not isinstance(value.action,Payment):
            return dict(verdict=verdict(hard),checks=hard,availability="not_required",model_calls=0,usage={},provider_latency_ms=None,evaluation=None)
        envelope=self.client.envelope(value,policy)
        try:
            record=self.usage.reserve(value.workspace_id,logical_key,attempt,resource_kind=resource_kind,resource_id=resource_id,
                pool="evaluation",backend=self.config.backend,model=self.config.model,request=envelope,
                allowance_tokens=self.config.allowance_tokens,timeout_s=self.config.timeout_s,resume_from_history=resume_from_history)
        except QuotaExceeded:
            return unavailable(hard,"quota_exhausted")
        except Conflict:
            return unavailable(hard,"request_identity_conflict")
        except ValueError:
            return unavailable(hard,"invalid_or_oversized_request")
        except LookupError:
            return unavailable(hard,"resource_unavailable")
        if record["state"] in {"settled","overrun"}:
            return self._cached(record,hard)
        if not self.usage.dispatch(value.workspace_id,record["id"]):
            current=self.usage.get(value.workspace_id,record["id"])
            if current["state"] in {"settled","overrun"}:
                return self._cached(current,hard)
            code={"reserved":"model_attempt_in_progress","dispatched":"model_attempt_in_progress",
                  "cancelled":"model_attempt_cancelled","unknown":"model_attempt_unknown"}[current["state"]]
            return {**unavailable(hard,code),"usage_request_id":record["id"]}
        try:
            evaluation=self.client.complete(envelope)
            judgment=evaluation["judgment"]
            semantic=dict(id="general_semantic",kind="semantic",outcome={"allow":"pass","review":"review","deny":"fail"}[judgment["verdict"]],
                          label="General semantic policy judgment",detail="; ".join(judgment["reason_codes"]))
            result=dict(verdict=verdict(hard+[semantic]),checks=hard+[semantic],availability="available",model_calls=1,
                usage=evaluation["usage"],provider_latency_ms=evaluation["latency_ms"],evaluation=evaluation,
                request_hash=identity(envelope),reused_answers=False)
            observed_usage,model=evaluation["usage"],evaluation["model"]
            code=None
        except GeneralJudgeError as error:
            result=unavailable(hard,error.code,1,error.usage,error.latency_ms)
            observed_usage,model,code=error.usage,error.model,error.code
        finished=self.usage.finish(value.workspace_id,record["id"],response=result,usage=observed_usage,resolved_model=model,error_code=code)
        if finished["state"]=="overrun":
            result=unavailable(hard,"usage_allowance_exceeded",1,observed_usage,result["provider_latency_ms"])
        return {**result,"usage_request_id":record["id"]}
