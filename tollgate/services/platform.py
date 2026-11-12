"""Application boundary for scoped REST, workers, and MCP clients.

Financial mutations lock the workspace row in PostgreSQL. This deliberately
serializes short financial transactions within one workspace, never network I/O.
"""
from __future__ import annotations

import copy
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from tollgate.auth import Identity, Principal, secret_hash
from tollgate.authorization import DEMO_INVOICES, Mandate, cents, fixtures
from tollgate.decision_context import DecisionInputV1, Payment, ProposalV1, canonical, identity, utcnow
from tollgate.evaluation.evaluator import evaluate_input, hard_checks, verdict
from tollgate.policy_config import migrate, validate
from tollgate.semantic_policy import PRESETS
from tollgate.storage import schema as s
from tollgate.storage.database import row, change
from tollgate.observability import traced


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


class Conflict(ValueError):
    pass


class QuotaExceeded(ValueError):
    pass


class Platform:
    def __init__(self, database, settings, local_token=None):
        self.db, self.settings = database, settings
        self.identity = Identity(database, settings, local_token)

    def audit(self, c, wid, actor, kind, resource, payload=None):
        payload = payload or {}
        c.execute(s.audit.insert().values(workspace_id=wid, id=new_id("audit"), actor_id=actor, kind=kind,
                  resource_id=resource, correlation_id=resource, created_at=time.time(), payload_hash=identity(payload), payload=payload))

    def authorize(self, c, p, wid, permission, mid=None):
        return self.identity.context(c, p, wid, permission, mid)

    def workspaces(self, p):
        if p.kind == "agent":
            return []
        with self.db.transaction() as c:
            return [dict(r) for r in c.execute(select(s.workspaces, s.memberships.c.role).join(s.memberships,
                s.memberships.c.workspace_id == s.workspaces.c.id).where(s.memberships.c.actor_id == p.actor_id,
                s.memberships.c.active.is_(True), s.workspaces.c.expires_at > time.time())).mappings()]

    def provision(self, p, request_id):
        if p.kind != "human":
            raise PermissionError("Human identity required")
        # Serialize provisioning across API instances using a precreated global row.
        with self.db.transaction() as c:
            c.execute(select(s.usage).where(s.usage.c.scope == "global").with_for_update()).first()
            old = c.execute(select(s.workspaces).where(s.workspaces.c.creator_id == p.actor_id,
                            s.workspaces.c.provision_key == request_id)).mappings().first()
            if old:
                return dict(old)
            own = c.execute(select(func.count()).select_from(s.workspaces).where(s.workspaces.c.creator_id == p.actor_id)).scalar_one()
            total = c.execute(select(func.count()).select_from(s.workspaces)).scalar_one()
            if own >= 3 or total >= 500:
                raise QuotaExceeded("Synthetic workspace provisioning limit reached")
            wid, generation = new_id("ws"), new_id("synthetic")
            timestamp = time.time()
            c.execute(s.workspaces.insert().values(id=wid, creator_id=p.actor_id, provision_key=request_id, generation=generation,
                      created_at=timestamp, expires_at=timestamp + 86400 * (30 if self.settings.mode == "local" else 1),
                      revision=1, active_policy_id=None))
            c.execute(s.memberships.insert().values(workspace_id=wid, actor_id=p.actor_id, role="owner", active=True))
            c.execute(s.usage.insert().values(scope=wid, calls=0, tokens=0))
            bundle = migrate(PRESETS[0])
            c.execute(s.policies.insert().values(workspace_id=wid, id=bundle["id"], content_hash=bundle["hash"], payload=bundle,
                      created_at=timestamp, admission="offline-fixture"))
            c.execute(s.workspaces.update().where(s.workspaces.c.id == wid).values(active_policy_id=bundle["id"]))
            vendors, invoices = fixtures()
            for vid in {invoices[i]["vendor_id"] for i in DEMO_INVOICES}:
                c.execute(s.vendors.insert().values(workspace_id=wid, id=vid, revision=1, payload=vendors[vid]))
            for iid in DEMO_INVOICES:
                item = invoices[iid]
                c.execute(s.invoices.insert().values(workspace_id=wid, id=iid, namespace=generation, source_id=iid,
                          vendor_id=item["vendor_id"], amount_minor=cents(item["amount_due_usd"]), revision=1,
                          evidence_revision=0, payload=item))
                if iid != "INV-7002":
                    body = (f"Receiving record for {iid}: delivery dispute remains unresolved; eight cartons missing."
                            if iid == "INV-6408" else f"Receiving record for {iid}: complete delivery confirmed and accepted.")
                    self._append_evidence(c, wid, iid, "fixture", body, "verified_receiving", None, initial=True)
            self.audit(c, wid, p.actor_id, "workspace_created", wid, {"generation": generation, "mode": "synthetic"})
            return dict(c.execute(select(s.workspaces).where(s.workspaces.c.id == wid)).mappings().one())

    @traced("tollgate.authority.create")
    def create_mandate(self, p, wid, invoice_ids, budget_minor, auto_limit_minor, expires_seconds=7200,
                       required_approvals=1, reviewers=None):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "operate")
            workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id == wid)).mappings().one()
            policy = row(c, s.policies, wid, workspace["active_policy_id"])
            if policy["admission"] != "offline-fixture":
                raise Conflict("Policy is not admitted for this backend")
            if not 0 <= auto_limit_minor <= budget_minor <= policy["payload"]["financial"]["budget_cents"] or budget_minor <= 0:
                raise ValueError("Invalid mandate limits")
            if auto_limit_minor > policy["payload"]["financial"]["auto_limit_cents"]:
                raise ValueError("Automatic limit exceeds pinned policy")
            if len(invoice_ids) != len(set(invoice_ids)) or not 1 <= len(invoice_ids) <= 20:
                raise ValueError("Select distinct approved invoices")
            selected = [row(c, s.invoices, wid, iid) for iid in invoice_ids]
            if any(i["namespace"] != workspace["generation"] for i in selected):
                raise Conflict("Source generation changed")
            beneficiaries = {}
            for item in selected:
                vendor = row(c, s.vendors, wid, item["vendor_id"])["payload"]
                beneficiaries[item["vendor_id"]] = {key: vendor[key] for key in ("bank_account", "routing_number")}
            reviewers = reviewers or [p.actor_id]
            if len(set(reviewers)) != len(reviewers) or not 1 <= required_approvals <= len(reviewers) <= 5:
                raise ValueError("Approval rule requires distinct assigned eligible reviewers")
            for actor in reviewers:
                self.authorize(c, Principal(actor), wid, "review")
            mid = new_id("mandate")
            stamp = utcnow()
            authority = Mandate(mandate_id=mid, invoice_ids=invoice_ids,
                approved_invoices={i["id"]: {"vendor_id": i["vendor_id"], "amount_cents": i["amount_minor"]} for i in selected},
                beneficiaries=beneficiaries, budget_cents=budget_minor, auto_limit_cents=auto_limit_minor,
                created_at=stamp, expires_at=stamp + timedelta(seconds=expires_seconds)).model_dump(mode="json")
            if policy["payload"]["require_confirmation"]:
                authority["task"] += " Require eligible receiving confirmation of complete delivery."
            payload = {"authority": authority, "invoices": {i["id"]: i["payload"] for i in selected},
                       "source_revisions": {i["id"]: i["revision"] for i in selected},
                       "approval_rule": {"reviewers": reviewers, "required_approvals": required_approvals}}
            c.execute(s.mandates.insert().values(workspace_id=wid, id=mid, revision=1, generation=workspace["generation"],
                policy_id=policy["id"], created_by=p.actor_id, created_at=stamp.timestamp(),
                expires_at=datetime.fromisoformat(authority["expires_at"]).timestamp(), revoked=False,
                budget_minor=budget_minor, auto_limit_minor=auto_limit_minor, spent_minor=0, reserved_minor=0, payload=payload))
            self.audit(c, wid, p.actor_id, "mandate_created", mid, {"policy_id": policy["id"], "budget_minor": budget_minor})
            return row(c, s.mandates, wid, mid)

    def issue_credential(self, p, wid, mid, expires_seconds=3600):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "operate")
            mandate = row(c, s.mandates, wid, mid)
            if mandate["revoked"] or mandate["expires_at"] <= time.time():
                raise Conflict("Mandate unavailable")
            cid, secret = new_id("credential"), "tg_" + secrets.token_urlsafe(32)
            c.execute(s.credentials.insert().values(workspace_id=wid, id=cid, secret_hash=secret_hash(secret),
                principal_id="agent:" + cid, mandate_id=mid, created_at=time.time(),
                expires_at=min(time.time() + expires_seconds, mandate["expires_at"]), revoked=False))
            self.audit(c, wid, p.actor_id, "credential_created", cid, {"mandate_id": mid})
            return {"id": cid, "agent_token": secret, "mandate_id": mid, "workspace_id": wid}

    def revoke_credential(self, p, wid, cid):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "operate")
            row(c, s.credentials, wid, cid)
            change(c, s.credentials, wid, cid, revoked=True)
            self.audit(c, wid, p.actor_id, "credential_revoked", cid, {"accepted_work_cancelled": False})

    def list_resources(self, p, wid, table, mid=None):
        allowed = {s.mandates, s.proposals, s.reviews, s.policies, s.credentials, s.invoices, s.vendors, s.audit,
                   s.experiments, s.shadows, s.shadow_rows, s.jobs, s.legacy, s.runs}
        if table not in allowed:
            raise ValueError("Unsupported resource")
        with self.db.transaction() as c:
            if p.kind == "agent":
                if table not in {s.invoices, s.vendors} or not mid:
                    raise LookupError("Resource unavailable")
                self.authorize(c, p, wid, "agent", mid)
            else:
                self.authorize(c, p, wid, "read")
            query = select(table).where(table.c.workspace_id == wid)
            if table in {s.invoices, s.vendors} and mid:
                mandate = row(c, s.mandates, wid, mid)
                if mandate["revoked"] or mandate["expires_at"] <= time.time():
                    raise LookupError("Resource unavailable")
                authority = mandate["payload"]["authority"]
                ids = authority["invoice_ids"] if table == s.invoices else list(authority["beneficiaries"])
                query = query.where(table.c.id.in_(ids))
            rows = [dict(r) for r in c.execute(query.limit(200)).mappings()]
            if table == s.credentials:
                for item in rows:
                    item.pop("secret_hash")
            if p.kind == "agent":
                self.audit(c, wid, p.actor_id, "scoped_resource_read", mid, {"resource_type": table.name})
                if table == s.vendors:
                    return [{"id": item["id"], "name": item["payload"]["name"], "beneficiary_ref": item["id"]} for item in rows]
                return [{"id": item["id"], "amount_minor": item["amount_minor"], "currency": "USD",
                         "beneficiary_ref": item["vendor_id"], "text": item["payload"]["text"]} for item in rows]
            return rows

    def capture(self, c, wid, proposal):
        mandate = row(c, s.mandates, wid, proposal["mandate_id"])
        body = ProposalV1.model_validate(proposal["payload"])
        action = body.action
        iid = getattr(action, "invoice_id", None)
        invoice = row(c, s.invoices, wid, iid, required=False) if iid else None
        authority = copy.deepcopy(mandate["payload"]["authority"])
        authority["revoked"] = mandate["revoked"]
        authority["expires_at"] = datetime.fromtimestamp(mandate["expires_at"], timezone.utc).isoformat()
        # Include only the proposed, approved invoice; never a workspace catalog.
        approved = mandate["payload"]["invoices"].get(iid)
        vendors = {vid: row(c, s.vendors, wid, vid)["payload"] for vid in authority["beneficiaries"]}
        evidence = []
        if iid in authority["invoice_ids"]:
            evidence = [r["payload"] for r in c.execute(select(s.evidence).where(s.evidence.c.workspace_id == wid,
                        s.evidence.c.invoice_id == iid).order_by(s.evidence.c.revision)).mappings()
                        if r["payload"].get("source_namespace") == mandate["generation"]]
        duplicate = False
        if invoice:
            claim = c.execute(select(s.claims).where(s.claims.c.workspace_id == wid, s.claims.c.namespace == invoice["namespace"],
                     s.claims.c.invoice_id == invoice["source_id"])).mappings().first()
            duplicate = bool(claim and claim["state"] != "released" and claim["proposal_id"] != proposal["id"])
        own = c.execute(select(s.reservations).where(s.reservations.c.workspace_id == wid,
              s.reservations.c.proposal_id == proposal["id"], s.reservations.c.state == "active")).mappings().first()
        policy = row(c, s.policies, wid, mandate["policy_id"])["payload"]
        return DecisionInputV1(workspace_id=wid, mandate_id=mandate["id"], proposal_id=proposal["id"],
            company_namespace=mandate["generation"], action=action, claims=body.claims, evaluation_time=utcnow(),
            authority_json=canonical(authority), policy_json=canonical(policy), policy_hash=policy["hash"],
            sources_json=canonical({"invoices": {iid: approved} if approved else {}, "vendors": vendors,
                "source_revision": invoice["revision"] if invoice else None,
                "pinned_source_revision": mandate["payload"]["source_revisions"].get(iid)}),
            financial_json=canonical({"spent_minor": mandate["spent_minor"],
                "reserved_minor": mandate["reserved_minor"] - (own["amount_minor"] if own else 0), "duplicate": duplicate}),
            evidence_json=canonical(evidence), mandate_revision=mandate["revision"],
            evidence_revision=invoice["evidence_revision"] if invoice else 0)

    def _save_input(self, c, wid, pid, value):
        iid = new_id("input")
        c.execute(s.inputs.insert().values(workspace_id=wid, id=iid, proposal_id=pid, input_hash=value.input_hash,
                  payload=value.model_dump(mode="json"), created_at=time.time()))
        change(c, s.proposals, wid, pid, input_id=iid)
        return iid

    def enqueue(self, c, wid, kind, resource_id, key):
        old = c.execute(select(s.jobs.c.id).where(s.jobs.c.workspace_id == wid, s.jobs.c.logical_key == key)).first()
        if old:
            return old[0]
        active = c.execute(select(func.count()).select_from(s.jobs).where(s.jobs.c.workspace_id == wid,
                 s.jobs.c.state.in_(["queued", "running"]),
                 s.jobs.c.kind.in_(["shadow", "experiment"]) if kind in {"shadow", "experiment"} else s.jobs.c.kind.not_in(["shadow", "experiment"]))).scalar_one()
        if active >= self.settings.max_jobs:
            raise QuotaExceeded("Workspace active-job limit reached")
        jid = new_id("job")
        c.execute(s.jobs.insert().values(workspace_id=wid, id=jid, kind=kind, object_id=resource_id, logical_key=key,
                  state="queued", attempts=0, available_at=time.time(), cancel_requested=False, created_at=time.time()))
        return jid

    @traced("tollgate.proposal.submit")
    def submit(self, p, wid, mid, body: ProposalV1, _operator=False):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "operate" if _operator else "agent", mid)
            mandate = row(c, s.mandates, wid, mid)
            old = c.execute(select(s.proposals).where(s.proposals.c.workspace_id == wid,
                s.proposals.c.mandate_id == mid, s.proposals.c.request_id == body.request_id)).mappings().first()
            fingerprint = identity(body)
            if old:
                if old["payload_hash"] != fingerprint:
                    raise Conflict("Request ID already identifies a different proposal payload")
                if old["principal_id"] != p.actor_id:
                    raise LookupError("Resource unavailable")
                return self._proposal_view(c, p, wid, dict(old))
            if body.claims:
                if self.settings.claims_mode == "off":
                    raise ValueError("Claims are disabled")
                ids = {r[0] for r in c.execute(select(s.evidence.c.id).where(s.evidence.c.workspace_id == wid,
                       s.evidence.c.invoice_id == getattr(body.action, "invoice_id", "")))}
                if any(claim.invoice_id != getattr(body.action, "invoice_id", None) or not set(claim.source_ids) <= ids for claim in body.claims):
                    raise ValueError("Claim sources are unavailable for this invoice")
                raise ValueError("Claim evaluation is unavailable in this release")
            pid = new_id("proposal")
            c.execute(s.proposals.insert().values(workspace_id=wid, id=pid, mandate_id=mid, request_id=body.request_id,
                principal_id=p.actor_id, credential_id=p.credential_id, payload_hash=fingerprint, action_hash=identity(body.action),
                payload=body.model_dump(mode="json"), state="evaluating", revision=1, rechecks=0, checks=[],
                created_at=time.time(), updated_at=time.time()))
            proposal = row(c, s.proposals, wid, pid)
            capture = self.capture(c, wid, proposal)
            self._save_input(c, wid, pid, capture)
            checks = hard_checks(capture)
            if verdict(checks) == "deny":
                change(c, s.proposals, wid, pid, state="denied", verdict="deny", checks=checks)
            elif not isinstance(body.action, Payment):
                change(c, s.proposals, wid, pid, state="confirmed", verdict="allow", checks=checks)
            else:
                self.enqueue(c, wid, "evaluate", pid, "evaluate:" + pid + ":0")
            self.audit(c, wid, p.actor_id, "proposal_accepted", pid, {"payload_hash": fingerprint, "credential_id": p.credential_id})
            return self._proposal_view(c, p, wid, row(c, s.proposals, wid, pid))

    def proposal(self, p, wid, pid):
        with self.db.transaction() as c:
            proposal = row(c, s.proposals, wid, pid)
            self.authorize(c, p, wid, "agent" if p.kind == "agent" else "read", proposal["mandate_id"])
            return self._proposal_view(c, p, wid, proposal)

    def _proposal_view(self, c, p, wid, proposal):
        if p.kind == "agent" and proposal["principal_id"] != p.actor_id:
            raise LookupError("Resource unavailable")
        receipt = c.execute(select(s.receipts).where(s.receipts.c.workspace_id == wid,
                    s.receipts.c.proposal_id == proposal["id"])).mappings().first()
        result = {"id": proposal["id"], "request_id": proposal["request_id"], "mandate_id": proposal["mandate_id"],
                  "verdict": proposal["verdict"], "state": proposal["state"], "executed": proposal["state"] == "confirmed",
                  "reason_codes": [ch["id"] for ch in proposal["checks"] if ch["outcome"] != "pass"],
                  "receipt_id": receipt["id"] if receipt else None,
                  "status_url": f"/api/v1/workspaces/{wid}/proposals/{proposal['id']}"}
        action = proposal["payload"]["action"]
        if action["tool"] != "schedule_payment" and proposal["state"] == "confirmed":
            mandate = row(c, s.mandates, wid, proposal["mandate_id"])
            result["executed"] = False
            result["result"] = (mandate["payload"]["invoices"][action["invoice_id"]] if action["tool"] == "read_invoice"
                                else {"id": action["vendor_id"], "name": row(c, s.vendors, wid, action["vendor_id"])["payload"]["name"]})
        if p.kind != "agent":
            result.update(proposal)
            result["receipt"] = dict(receipt) if receipt else None
            result["input"] = row(c, s.inputs, wid, proposal["input_id"])
            history = [dict(r) for r in c.execute(select(s.inputs).where(s.inputs.c.workspace_id == wid,
                       s.inputs.c.proposal_id == proposal["id"]).order_by(s.inputs.c.created_at)).mappings()]
            result["input_history"] = history
            result["evaluations"] = [dict(r) for r in c.execute(select(s.evaluations).where(s.evaluations.c.workspace_id == wid,
                  s.evaluations.c.input_id.in_([i["id"] for i in history]))).mappings()]
        return result

    def receipt(self, p, wid, rid):
        with self.db.transaction() as c:
            receipt = row(c, s.receipts, wid, rid)
            proposal = row(c, s.proposals, wid, receipt["proposal_id"])
            self.authorize(c, p, wid, "agent" if p.kind == "agent" else "read", proposal["mandate_id"])
            if p.kind == "agent" and proposal["principal_id"] != p.actor_id:
                raise LookupError("Resource unavailable")
            return receipt

    @staticmethod
    def context_identity(value):
        return identity({k: v for k, v in value.model_dump(mode="json").items() if k != "evaluation_time"})

    @traced("tollgate.semantic.evaluate")
    def evaluate_proposal(self, wid, pid):
        with self.db.transaction(wid) as c:
            proposal = row(c, s.proposals, wid, pid)
            if proposal["state"] != "evaluating":
                return
            capture = DecisionInputV1.model_validate(row(c, s.inputs, wid, proposal["input_id"])["payload"])
            latest = self.capture(c, wid, proposal)
            # Queue delay is not an in-flight model attempt. Refresh and persist the
            # input before sending it, rather than spending the bounded stale-call
            # retries on unrelated payments completed while this job was waiting.
            if self.context_identity(capture) != self.context_identity(latest):
                proposal["input_id"] = self._save_input(c, wid, pid, latest)
                capture = latest
        # No financial lock or write transaction survives this boundary.
        result = evaluate_input(capture)
        with self.db.transaction(wid) as c:
            proposal = row(c, s.proposals, wid, pid)
            if proposal["state"] != "evaluating":
                return
            current = self.capture(c, wid, proposal)
            stale = self.context_identity(capture) != self.context_identity(current)
            c.execute(s.evaluations.insert().values(workspace_id=wid, id=new_id("evaluation"), input_id=proposal["input_id"],
                request_hash=result["request_hash"], backend="offline", availability=result["availability"], stale=stale,
                payload=result, created_at=time.time()))
            checks = hard_checks(current)
            if verdict(checks) == "deny":
                change(c, s.proposals, wid, pid, state="denied", verdict="deny", checks=checks)
            elif stale:
                if proposal["rechecks"] >= 2:
                    change(c, s.proposals, wid, pid, state="held", verdict="review", checks=[dict(id="stale_context", kind="authority",
                        outcome="unavailable", label="Context changed repeatedly", detail="Submit an explicit reassessment")])
                else:
                    self._save_input(c, wid, pid, current)
                    n = proposal["rechecks"] + 1
                    change(c, s.proposals, wid, pid, rechecks=n)
                    self.enqueue(c, wid, "evaluate", pid, f"evaluate:{pid}:{n}")
            else:
                combined = checks + [ch for ch in result["checks"] if ch["kind"] == "semantic"]
                outcome = verdict(combined)
                change(c, s.proposals, wid, pid, verdict=outcome, checks=combined, updated_at=time.time())
                if outcome == "deny":
                    change(c, s.proposals, wid, pid, state="denied")
                else:
                    self._reserve(c, wid, proposal, current)
                    if outcome == "review":
                        self._review(c, wid, proposal, current)
                    else:
                        self._intent(c, wid, pid)
                self.audit(c, wid, "service:evaluator", "proposal_decided", pid, {"verdict": outcome, "input_hash": capture.input_hash})
                self._capture_shadows(c, wid, proposal, capture)

    @traced("tollgate.reservation.create")
    def _reserve(self, c, wid, proposal, capture):
        action = capture.action
        if not isinstance(action, Payment):
            raise ValueError("Read actions cannot reserve money")
        mandate = row(c, s.mandates, wid, proposal["mandate_id"])
        invoice = row(c, s.invoices, wid, action.invoice_id)
        key = (s.claims.c.workspace_id == wid, s.claims.c.namespace == invoice["namespace"], s.claims.c.invoice_id == invoice["source_id"])
        claim = c.execute(select(s.claims).where(*key)).mappings().first()
        if claim and claim["state"] != "released":
            raise Conflict("Obligation already claimed")
        if claim:
            c.execute(s.claims.update().where(*key).values(proposal_id=proposal["id"], state="active"))
        else:
            c.execute(s.claims.insert().values(workspace_id=wid, namespace=invoice["namespace"], invoice_id=invoice["source_id"],
                      proposal_id=proposal["id"], state="active"))
        c.execute(s.reservations.insert().values(workspace_id=wid, id=new_id("reservation"), proposal_id=proposal["id"],
                  mandate_id=mandate["id"], amount_minor=action.amount_minor, state="active"))
        change(c, s.mandates, wid, mandate["id"], reserved_minor=mandate["reserved_minor"] + action.amount_minor)

    @traced("tollgate.reservation.release")
    def _release(self, c, wid, proposal, reason, committed=False):
        reservation = c.execute(select(s.reservations).where(s.reservations.c.workspace_id == wid,
                      s.reservations.c.proposal_id == proposal["id"])).mappings().first()
        if not reservation or reservation["state"] != "active":
            return
        mandate = row(c, s.mandates, wid, proposal["mandate_id"])
        amount = reservation["amount_minor"]
        change(c, s.mandates, wid, mandate["id"], reserved_minor=mandate["reserved_minor"] - amount,
               spent_minor=mandate["spent_minor"] + (amount if committed else 0))
        change(c, s.reservations, wid, reservation["id"], state="committed" if committed else "released", release_reason=reason)
        c.execute(s.claims.update().where(s.claims.c.workspace_id == wid, s.claims.c.proposal_id == proposal["id"]).values(
                  state="confirmed" if committed else "released"))

    def _review(self, c, wid, proposal, capture):
        rule = row(c, s.mandates, wid, proposal["mandate_id"])["payload"]["approval_rule"]
        c.execute(s.reviews.insert().values(workspace_id=wid, id=new_id("review"), proposal_id=proposal["id"], state="awaiting_review",
            revision=1, assigned_to=rule["reviewers"][0], required_approvals=rule["required_approvals"], due_at=time.time()+86400,
            binding_hash=self.binding(capture), input_id=proposal["input_id"], rule=rule))
        change(c, s.proposals, wid, proposal["id"], state="awaiting_review")

    @staticmethod
    def binding(value):
        return identity({"proposal": value.proposal_id, "action": value.action.model_dump(mode="json"), "claims": [c.model_dump(mode="json") for c in value.claims],
            "authority": value.authority_json, "mandate_revision": value.mandate_revision, "policy_hash": value.policy_hash,
            "sources": value.sources_json, "evidence": value.evidence_json, "evidence_revision": value.evidence_revision})

    def _intent(self, c, wid, pid):
        intent_id = new_id("intent")
        c.execute(s.intents.insert().values(workspace_id=wid, id=intent_id, proposal_id=pid, provider_key=new_id("tollgate"), state="queued"))
        change(c, s.proposals, wid, pid, state="queued")
        self.enqueue(c, wid, "execute", intent_id, "execute:" + intent_id)

    @traced("tollgate.review.resolve")
    def resolve(self, p, wid, rid, resolution, expected_revision, comment=""):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "review")
            review = row(c, s.reviews, wid, rid)
            if p.actor_id not in review["rule"]["reviewers"]:
                raise LookupError("Resource unavailable")
            if review["revision"] != expected_revision or review["state"] != "awaiting_review":
                raise Conflict("Review revision/state changed")
            if resolution not in {"approve", "reject"}:
                raise ValueError("Invalid resolution")
            proposal = row(c, s.proposals, wid, review["proposal_id"])
            current = self.capture(c, wid, proposal)
            checks = hard_checks(current)
            if self.binding(current) != review["binding_hash"] or verdict(checks) == "deny":
                self._cancel(c, wid, proposal, "stale_review", review_state="stale")
                return {"state": "stale"}
            if any(ch["outcome"] == "unavailable" for ch in proposal["checks"]) and resolution == "approve":
                raise Conflict("Required semantic evaluation unavailable; reassess before approval")
            if c.execute(select(s.resolutions.c.id).where(s.resolutions.c.workspace_id == wid,
                s.resolutions.c.review_id == rid, s.resolutions.c.actor_id == p.actor_id)).first():
                raise Conflict("This actor already resolved this review")
            c.execute(s.resolutions.insert().values(workspace_id=wid, id=new_id("resolution"), review_id=rid, actor_id=p.actor_id,
                resolution=resolution, comment=comment, binding_hash=review["binding_hash"], created_at=time.time()))
            change(c, s.reviews, wid, rid, revision=review["revision"] + 1)
            if resolution == "reject":
                self._cancel(c, wid, proposal, "review_rejected", review_state="rejected")
            else:
                approvals = c.execute(select(s.resolutions.c.actor_id).where(s.resolutions.c.workspace_id == wid,
                    s.resolutions.c.review_id == rid, s.resolutions.c.resolution == "approve")).scalars().all()
                # Membership eligibility is checked again even for an earlier approver.
                for actor in approvals:
                    self.authorize(c, Principal(actor), wid, "review")
                if len(approvals) >= review["required_approvals"]:
                    change(c, s.reviews, wid, rid, state="approved")
                    self._intent(c, wid, proposal["id"])
            self.audit(c, wid, p.actor_id, "review_resolved", rid, {"resolution": resolution, "binding_hash": review["binding_hash"]})
            return row(c, s.reviews, wid, rid)

    def _cancel(self, c, wid, proposal, reason, review_state="cancelled"):
        # Dispatched/unknown operations retain claims and reservations for reconciliation.
        intent = c.execute(select(s.intents).where(s.intents.c.workspace_id == wid,
                         s.intents.c.proposal_id == proposal["id"])).mappings().first()
        if intent and intent["state"] in {"dispatching", "submitted", "unknown", "confirmed"}:
            return False
        self._release(c, wid, proposal, reason)
        change(c, s.proposals, wid, proposal["id"], state="cancelled", updated_at=time.time())
        if intent:
            change(c, s.intents, wid, intent["id"], state="cancelled")
        c.execute(s.reviews.update().where(s.reviews.c.workspace_id == wid, s.reviews.c.proposal_id == proposal["id"],
                   s.reviews.c.state.in_(["awaiting_review", "approved"])).values(state=review_state, revision=s.reviews.c.revision + 1))
        self.audit(c, wid, "service:authority", reason, proposal["id"])
        return True

    @traced("tollgate.authority.revoke")
    def revoke_mandate(self, p, wid, mid):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "operate")
            mandate = row(c, s.mandates, wid, mid)
            change(c, s.mandates, wid, mid, revoked=True, revision=mandate["revision"] + 1)
            for proposal in c.execute(select(s.proposals).where(s.proposals.c.workspace_id == wid, s.proposals.c.mandate_id == mid,
                      s.proposals.c.state.in_(["evaluating", "awaiting_review", "queued", "held"]))).mappings().all():
                self._cancel(c, wid, dict(proposal), "mandate_revoked")
            self.audit(c, wid, p.actor_id, "mandate_revoked", mid)

    def _append_evidence(self, c, wid, iid, actor, body, source_type, supersedes_id, initial=False):
        invoice = row(c, s.invoices, wid, iid)
        if len(body.strip()) < 1 or len(body) > 6000 or source_type not in {"supplier", "operator_note", "verified_receiving"}:
            raise ValueError("Invalid evidence")
        records = c.execute(select(s.evidence).where(s.evidence.c.workspace_id == wid, s.evidence.c.invoice_id == iid)).mappings().all()
        if sum(r["payload"].get("source_namespace") == invoice["namespace"] for r in records) >= 12:
            raise QuotaExceeded("Invoice evidence limit reached")
        if supersedes_id:
            target = next((r for r in records if r["id"] == supersedes_id), None)
            if not target or target["payload"].get("source_namespace") != invoice["namespace"] or target["source_type"] != "verified_receiving" or source_type != "verified_receiving":
                raise ValueError("Only an eligible receiving correction may supersede a record for this invoice")
        eid, revision = new_id("evidence"), invoice["evidence_revision"] + 1
        payload = dict(evidence_id=eid, invoice_id=iid, source_namespace=invoice["namespace"], source_type=source_type, body=body, supersedes_id=supersedes_id,
                       added_by=actor, recorded_at=utcnow().isoformat(), content_hash=identity(body),
                       verification="Synthetic receiving fixture" if initial else "Operator assertion; no independent source authentication")
        c.execute(s.evidence.insert().values(workspace_id=wid, id=eid, invoice_id=iid, revision=revision, source_type=source_type,
                  supersedes_id=supersedes_id, actor_id=actor, body_hash=identity(body), payload=payload))
        change(c, s.invoices, wid, iid, evidence_revision=revision)
        return payload

    def append_evidence(self, p, wid, iid, body, source_type, supersedes_id=None):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "operate")
            record = self._append_evidence(c, wid, iid, p.actor_id, body, source_type, supersedes_id)
            for proposal in c.execute(select(s.proposals).where(s.proposals.c.workspace_id == wid,
                      s.proposals.c.state.in_(["awaiting_review", "queued"]))).mappings().all():
                if proposal["payload"]["action"].get("invoice_id") == iid:
                    self._cancel(c, wid, dict(proposal), "evidence_changed", review_state="stale")
            self.audit(c, wid, p.actor_id, "evidence_added", record["evidence_id"], {"invoice_id": iid, "content_hash": record["content_hash"]})
            return record

    def _capture_shadows(self, c, wid, proposal, capture):
        for sub in c.execute(select(s.shadows).where(s.shadows.c.workspace_id == wid, s.shadows.c.enabled.is_(True))).mappings():
            # Separate queue entries; no model or operational mutations here.
            if sub['created_at'] > proposal['created_at']:
                continue
            try:
                self.enqueue(c, wid, "shadow", proposal["input_id"], f"shadow:{sub['id']}:{proposal['input_id']}")
            except QuotaExceeded:
                self.audit(c, wid, "service:shadow", "shadow_quota_skipped", proposal["id"])


    def reassess(self, p, wid, pid):
        with self.db.transaction() as c:
            self.authorize(c, p, wid, "operate")
            old = row(c, s.proposals, wid, pid)
            if old["payload"]["action"]["tool"] != "schedule_payment":
                raise ValueError("Select a payment proposal")
            invoice = row(c, s.invoices, wid, old["payload"]["action"]["invoice_id"])
            body = ProposalV1.model_validate({**old["payload"], "request_id": "reassess:"+pid+":"+str(invoice["evidence_revision"])+":"+identity(p.actor_id)[:12]})
        result = self.submit(p, wid, old["mandate_id"], body, _operator=True)
        with self.db.transaction(wid) as c:
            self.audit(c, wid, p.actor_id, "proposal_reassessed", result["id"], {"parent_proposal_id": pid})
        return result

    def reset_synthetic(self, p, wid, expected_revision):
        with self.db.transaction(wid) as c:
            self.authorize(c, p, wid, "admin")
            workspace = c.execute(select(s.workspaces).where(s.workspaces.c.id == wid)).mappings().one()
            if workspace["revision"] != expected_revision:
                raise Conflict("Workspace revision changed")
            ambiguous = c.execute(select(s.intents.c.id).where(s.intents.c.workspace_id == wid,
                s.intents.c.state.in_(["dispatching", "submitted", "unknown"]))).first()
            if ambiguous:
                raise Conflict("Reconcile dispatched/unknown executions before resetting")
            resets = c.execute(select(func.count()).select_from(s.audit).where(s.audit.c.workspace_id == wid,
                                s.audit.c.kind == "workspace_reset")).scalar_one()
            if resets >= 5:
                raise QuotaExceeded("Synthetic reset limit reached; usage quotas are never reset")
            for proposal in c.execute(select(s.proposals).where(s.proposals.c.workspace_id == wid,
                    s.proposals.c.state.in_(["evaluating", "queued", "awaiting_review", "held"]))).mappings().all():
                self._cancel(c, wid, dict(proposal), "workspace_reset")
            c.execute(s.mandates.update().where(s.mandates.c.workspace_id == wid).values(revoked=True, revision=s.mandates.c.revision + 1))
            c.execute(s.credentials.update().where(s.credentials.c.workspace_id == wid).values(revoked=True))
            c.execute(s.runs.update().where(s.runs.c.workspace_id == wid,
                s.runs.c.state.not_in(["finished", "cancelled", "failed"])).values(state="cancelled", revision=s.runs.c.revision + 1))
            c.execute(s.jobs.update().where(s.jobs.c.workspace_id == wid,
                s.jobs.c.state.in_(["queued", "running"])).values(cancel_requested=True))
            generation = new_id("synthetic")
            c.execute(s.workspaces.update().where(s.workspaces.c.id == wid).values(generation=generation, revision=expected_revision+1))
            vendors, invoices = fixtures()
            for item in c.execute(select(s.vendors).where(s.vendors.c.workspace_id == wid)).mappings().all():
                change(c, s.vendors, wid, item["id"], revision=item["revision"]+1, payload=vendors[item["id"]])
            for item in c.execute(select(s.invoices).where(s.invoices.c.workspace_id == wid)).mappings().all():
                iid = item["id"]
                change(c, s.invoices, wid, iid, namespace=generation, revision=item["revision"]+1, payload=invoices[iid])
                if iid != "INV-7002":
                    body = (f"Receiving record for {iid}: delivery dispute remains unresolved; eight cartons missing."
                            if iid == "INV-6408" else f"Receiving record for {iid}: complete delivery confirmed and accepted.")
                    self._append_evidence(c, wid, iid, "fixture", body, "verified_receiving", None, initial=True)
            self.audit(c, wid, p.actor_id, "workspace_reset", wid, {"previous_generation": workspace["generation"], "generation": generation})
            return {"workspace_id": wid, "generation": generation, "revision": expected_revision+1, "usage_reset": False}
