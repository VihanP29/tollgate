"""Operator-owned append-only evidence and linked payment reassessment."""
import copy
import uuid
from tollgate.semantic_policy import hashed

def active_records(session,invoice_id):
 records=session.evidence.get(invoice_id,[])
 superseded={r.get('supersedes_id') for r in records if r['source_type']=='verified_receiving'}
 return [r for r in records if r['evidence_id'] not in superseded]

def append_evidence(gateway,sid,iid,body,source_type,supersedes_id=None):
 from tollgate.authorization import now
 if source_type not in {'supplier','operator_note','verified_receiving'} or not body.strip() or len(body)>6000: raise ValueError('Invalid evidence source or length')
 with gateway.lock:
  session=gateway.get(sid).model_copy(deep=True)
  if not session.policy: raise ValueError('Create a showcase mandate for evidence resolution')
  if iid not in session.invoices: raise ValueError('Invoice is outside this mandate')
  records=session.evidence.setdefault(iid,[])
  if len(records)>=12: raise ValueError('Evidence limit reached')
  if supersedes_id and not any(r['evidence_id']==supersedes_id and r['source_type']=='verified_receiving' for r in records): raise ValueError('Unknown receiving record to supersede')
  if supersedes_id and source_type!='verified_receiving': raise ValueError('Only verified receiving corrections may supersede records')
  record=dict(evidence_id='EVD-'+uuid.uuid4().hex[:10],invoice_id=iid,kind='correspondence',source_type=source_type,source_record_id='local-demo-record',body=body,recorded_at=now().isoformat(),effective_at=now().isoformat(),added_by='local_operator',supersedes_id=supersedes_id,content_hash=hashed(body),verification='Simulated operator verification; no external identity integration')
  records.append(record)
  for event in session.events:
   if event.args.get('memo')==iid and event.execution=='pending':
    event.execution='cancelled';event.resolution='cancelled';event.resolved_at=now();event.reasons=['Evidence changed; submit a fresh reassessment']
  gateway._commit(session,'evidence_added',record)
  return record

def reassess(gateway,sid,eid):
 from tollgate.authorization import Proposal
 with gateway.lock:
  session=gateway.get(sid)
  old=next((e for e in session.events if e.event_id==eid),None)
  if not old or old.tool!='schedule_payment' or not session.policy: raise ValueError('Select a showcase payment decision')
  revision=hashed(session.evidence.get(old.args['memo'],[]))
  request_id='reassess-'+hashed([eid,revision])[:40]
  result=gateway.submit(sid,Proposal(request_id=request_id,tool='schedule_payment',args=copy.deepcopy(old.args)), '',origin='operator_reassessment',_operator=True,parent_event_id=eid)
  return result
