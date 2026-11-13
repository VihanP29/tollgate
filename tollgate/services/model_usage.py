"""Durable model allowances. Provider I/O never runs in these transactions.

A dispatched attempt is charged even if its result is lost. Unknown token usage
consumes the whole allowance until a real response supplies authoritative usage.
"""
from __future__ import annotations
import re
import time
from sqlalchemy import select
from tollgate.decision_context import canonical, identity
from tollgate.services.platform import Conflict, QuotaExceeded, new_id
from tollgate.storage import schema as s
from tollgate.storage.database import row, change

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def token_total(usage):
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
        raise ValueError("Missing token usage")
    if set(usage) - set(TOKEN_FIELDS) or any(type(v) is not int or v < 0 for v in usage.values()):
        raise ValueError("Invalid token usage")
    total = sum(usage.values())
    if total > 2**63-1: raise ValueError("Token usage exceeds supported integer range")
    return total


class ModelUsage:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    def get(self,wid,rid):
        with self.db.transaction() as c:
            return row(c,s.model_requests,wid,rid)

    def summary(self,wid):
        with self.db.transaction() as c:
            records={r["scope"]:dict(r) for r in c.execute(select(s.usage).where(s.usage.c.scope.in_([wid,wid+":evaluation"]))).mappings()}
        if wid not in records: raise LookupError("Workspace usage unavailable")
        result={}
        for pool,scope in (("total",wid),("evaluation",wid+":evaluation")):
            current=records.get(scope,{"calls":0,"tokens":0,"reserved_calls":0,"reserved_tokens":0})
            calls,tokens=self._limits(scope)
            result[pool]={**{key:current[key] for key in ("calls","tokens","reserved_calls","reserved_tokens")},
                "call_limit":calls,"token_limit":tokens,
                "remaining_calls":max(0,calls-current["calls"]-current["reserved_calls"]),
                "remaining_tokens":max(0,tokens-current["tokens"]-current["reserved_tokens"])}
        return {"workspace":result,"global_caps_also_apply":True,"evaluation_fraction":self.settings.evaluation_usage_fraction}

    def _lock(self, c, wid, pool):
        # Match worker/provisioning lock order: global counters first.
        c.execute(select(s.usage).where(s.usage.c.scope == "global").with_for_update()).one()
        scopes = ["global", wid]
        if pool == "evaluation":
            scopes += ["global:evaluation", wid+":evaluation"]
        records = []
        for scope in scopes:
            record = c.execute(select(s.usage).where(s.usage.c.scope == scope).with_for_update()).mappings().first()
            if record is None:
                if scope == wid:
                    raise LookupError("Workspace usage unavailable")
                c.execute(s.usage.insert().values(scope=scope, calls=0, tokens=0))
                record = c.execute(select(s.usage).where(s.usage.c.scope == scope)).mappings().one()
            records.append(dict(record))
        return records

    def _limits(self, scope):
        global_scope = scope in {"global", "global:evaluation"}
        calls = self.settings.global_calls if global_scope else self.settings.workspace_calls
        tokens = self.settings.global_tokens if global_scope else self.settings.workspace_tokens
        if scope.endswith(":evaluation"):
            calls = int(calls * self.settings.evaluation_usage_fraction)
            tokens = int(tokens * self.settings.evaluation_usage_fraction)
        return calls, tokens

    @staticmethod
    def _adjust(c, records, **deltas):
        for record in records:
            c.execute(s.usage.update().where(s.usage.c.scope == record["scope"]).values(
                **{field: s.usage.c[field] + delta for field, delta in deltas.items()}))

    @staticmethod
    def _event(c, wid, request_id, kind, **payload):
        from tollgate.observability import operational_event
        operational_event("model_usage_transition_attempt",workspace_id=wid,resource_id=request_id,state=kind)
        c.execute(s.model_usage_events.insert().values(workspace_id=wid, id=new_id("usage_event"), request_id=request_id,
            kind=kind, created_at=time.time(), payload=payload))

    def reserve(self, wid, logical_key, attempt, *, resource_kind, resource_id, pool, backend, model, request, allowance_tokens, timeout_s,
                resume_from_history=False):
        if (not isinstance(logical_key, str) or not 1 <= len(logical_key) <= 240 or type(attempt) is not int
                or not 1 <= attempt <= self.settings.max_retries+1 or type(allowance_tokens) is not int or allowance_tokens <= 0
                or pool not in {"operational", "evaluation"} or type(timeout_s) not in {int,float}
                or not 0 < timeout_s <= self.settings.provider_timeout_s
                or not isinstance(model,str) or not 1 <= len(model) <= 120
                or not isinstance(backend,str) or not re.fullmatch(r"[a-z0-9_-]{1,80}",backend)
                or type(resume_from_history) is not bool):
            raise ValueError("Invalid model allowance")
        tables = {"proposal": s.proposals, "experiment": s.experiments, "shadow": s.shadows}
        if resource_kind not in {*tables, "workspace"}:
            raise ValueError("Invalid model resource")
        if not isinstance(request,dict):
            raise ValueError("Compiled model request must be an object")
        if len(canonical(request).encode()) > self.settings.max_request_bytes:
            raise ValueError("Compiled model request too large")
        fingerprint = identity(dict(request=request, backend=backend, model=model, pool=pool,
                                    resource_kind=resource_kind, resource_id=resource_id, allowance_tokens=allowance_tokens,
                                    timeout_s=timeout_s))
        with self.db.transaction() as c:
            records = self._lock(c,wid,pool)
            workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id==wid)).mappings().first()
            if not workspace or workspace["expires_at"] <= time.time():
                raise LookupError("Workspace unavailable")
            if resource_kind == "workspace":
                if resource_id != wid: raise LookupError("Resource unavailable")
            else:
                row(c,tables[resource_kind],wid,resource_id)
            previous = [dict(r) for r in c.execute(select(s.model_requests).where(s.model_requests.c.workspace_id==wid,
                s.model_requests.c.logical_key==logical_key).order_by(s.model_requests.c.attempt)).mappings()]
            if resume_from_history:
                # A job retry is an upper bound on model attempts, not proof that
                # every case was reached on earlier deliveries. Choose under the
                # same lock that reserves capacity and deduplicates dispatch.
                attempt = min(attempt,previous[-1]["attempt"]+1) if previous else 1
            if previous:
                if any(r["request_hash"] != fingerprint for r in previous):
                    raise Conflict("Model request identity changed under the same logical key")
                old = next((r for r in previous if r["attempt"]==attempt),None)
                if old: return old
                # A worker may die after response persistence but before saving its
                # experiment row. Reuse available results rather than billing a retry.
                if previous[-1]["state"] == "settled" and previous[-1]["response"].get("availability") == "available":
                    return previous[-1]
                if attempt != previous[-1]["attempt"]+1 or previous[-1]["state"] not in {"unknown", "settled", "overrun", "cancelled"}:
                    raise Conflict("Previous model attempt is still pending or retry sequence is invalid")
            elif attempt != 1:
                raise Conflict("Model attempts start at one")
            for record in records:
                max_calls,max_tokens = self._limits(record["scope"])
                if record["calls"]+record["reserved_calls"]+1 > max_calls or record["tokens"]+record["reserved_tokens"]+allowance_tokens > max_tokens:
                    raise QuotaExceeded("Model usage allowance exhausted")
            self._adjust(c,records,reserved_calls=1,reserved_tokens=allowance_tokens)
            rid = new_id("model_request")
            c.execute(s.model_requests.insert().values(workspace_id=wid,id=rid,logical_key=logical_key,attempt=attempt,
                resource_kind=resource_kind,resource_id=resource_id,pool=pool,backend=backend,requested_model=model,
                request_hash=fingerprint,request_payload=request,allowance_tokens=allowance_tokens,state="reserved",
                charged_tokens=0,created_at=time.time(),deadline=time.time()+timeout_s+5,timeout_s=timeout_s))
            self._event(c,wid,rid,"reserved",allowance_tokens=allowance_tokens,attempt=attempt)
            return row(c,s.model_requests,wid,rid)

    def dispatch(self, wid, rid):
        with self.db.transaction() as c:
            request = row(c,s.model_requests,wid,rid)
            records = self._lock(c,wid,request["pool"])
            request = row(c,s.model_requests,wid,rid)
            if request["state"] != "reserved": return False
            # Cancellation and reset use this same short workspace barrier. Once
            # this marker commits, a request is in flight and cannot be refunded.
            workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id==wid).with_for_update()).mappings().one()
            cancelled = False
            if request["resource_kind"] == "experiment":
                cancelled = row(c,s.experiments,wid,request["resource_id"])["state"] not in {"queued","running"}
            elif request["resource_kind"] == "shadow":
                cancelled = not row(c,s.shadows,wid,request["resource_id"])["enabled"]
            if cancelled or request["deadline"] <= time.time() or workspace["expires_at"] <= time.time():
                self._adjust(c,records,reserved_calls=-1,reserved_tokens=-request["allowance_tokens"])
                code = "resource_cancelled" if cancelled else "reservation_expired"
                change(c,s.model_requests,wid,rid,state="cancelled",error_code=code)
                self._event(c,wid,rid,"cancelled",error_code=code)
                return False
            self._adjust(c,records,calls=1,reserved_calls=-1)
            change(c,s.model_requests,wid,rid,state="dispatched",dispatched_at=time.time(),deadline=time.time()+request["timeout_s"]+5)
            self._event(c,wid,rid,"dispatched")
            return True

    def finish(self, wid, rid, *, response, usage=None, resolved_model=None, error_code=None):
        if not isinstance(response,dict):
            raise ValueError("Normalized response must be an object")
        actual = token_total(usage) if usage is not None else None
        if error_code is not None and not re.fullmatch(r"[a-z0-9_]{1,80}",error_code):
            raise ValueError("Invalid safe error code")
        if resolved_model is not None and (not isinstance(resolved_model,str) or len(resolved_model)>120):
            raise ValueError("Invalid model identity")
        if len(canonical(response).encode())>self.settings.max_request_bytes:
            raise ValueError("Normalized response too large")
        response_hash=identity(dict(response=response,usage=usage,resolved_model=resolved_model,error_code=error_code))
        with self.db.transaction() as c:
            request=row(c,s.model_requests,wid,rid)
            records=self._lock(c,wid,request["pool"])
            request=row(c,s.model_requests,wid,rid)
            if request["state"] in {"settled","overrun"}:
                if request["response_hash"] != response_hash: raise Conflict("Model attempt already has a different result")
                return request
            if request["state"] == "unknown" and actual is None and request["response_hash"]:
                if request["response_hash"] != response_hash: raise Conflict("Unknown attempt already has a different result")
                return request
            if request["state"] not in {"dispatched","unknown"}:
                raise Conflict("Only dispatched model attempts can finish")
            charged=actual if actual is not None else request["allowance_tokens"]
            state="unknown" if actual is None else "overrun" if charged>request["allowance_tokens"] else "settled"
            held_tokens=request["allowance_tokens"] if request["state"]=="dispatched" else 0
            self._adjust(c,records,tokens=charged-request["charged_tokens"],reserved_tokens=-held_tokens)
            change(c,s.model_requests,wid,rid,state=state,charged_tokens=charged,usage=usage,response_hash=response_hash,
                   response=response,resolved_model=resolved_model,error_code=error_code or ("usage_allowance_exceeded" if state=="overrun" else None))
            self._event(c,wid,rid,state,charged_tokens=charged,error_code=error_code)
            return row(c,s.model_requests,wid,rid)

    def cancel(self,wid,rid):
        with self.db.transaction() as c:
            request=row(c,s.model_requests,wid,rid)
            records=self._lock(c,wid,request["pool"])
            request=row(c,s.model_requests,wid,rid)
            if request["state"]=="cancelled": return
            if request["state"]!="reserved": raise Conflict("Dispatched usage cannot be cancelled")
            self._adjust(c,records,reserved_calls=-1,reserved_tokens=-request["allowance_tokens"])
            change(c,s.model_requests,wid,rid,state="cancelled")
            self._event(c,wid,rid,"cancelled")

    def recover(self):
        with self.db.transaction() as c:
            expired=[dict(r) for r in c.execute(select(s.model_requests).where(s.model_requests.c.state.in_(["reserved","dispatched"]),
                      s.model_requests.c.deadline<=time.time()).limit(100)).mappings()]
        for request in expired:
            # Recheck the marker after acquiring the same counter lock as dispatch.
            with self.db.transaction() as c:
                records=self._lock(c,request["workspace_id"],request["pool"])
                current=row(c,s.model_requests,request["workspace_id"],request["id"])
                if current["state"] not in {"reserved","dispatched"} or current["deadline"]>time.time(): continue
                if current["state"]=="reserved":
                    self._adjust(c,records,reserved_calls=-1,reserved_tokens=-current["allowance_tokens"])
                    change(c,s.model_requests,request["workspace_id"],request["id"],state="cancelled",error_code="reservation_expired")
                else:
                    self._adjust(c,records,tokens=current["allowance_tokens"],reserved_tokens=-current["allowance_tokens"])
                    change(c,s.model_requests,request["workspace_id"],request["id"],state="unknown",charged_tokens=current["allowance_tokens"],error_code="model_outcome_unknown")
                self._event(c,request["workspace_id"],request["id"],"recovered",previous_state=current["state"])
        return bool(expired)
