import copy
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from tollgate.authorization import AuthorizationGateway, Proposal, SemanticReviewer, now
from tollgate.evidence import append_evidence, reassess
from tollgate.semantic_policy import PRESETS, QUESTIONS, evaluate, validate_policy
from tollgate.replay import run_replay
from tollgate.demo_server import create_control_app
from tollgate.clients.jev import JevClient, JevError

def setup():
 g=AuthorizationGateway();s,t=g.create(policy=PRESETS[0]);return g,s,t

def pay(iid='INV-7002',vendor='V-HOOLI',amount=2240):
 return Proposal(tool='schedule_payment',args=dict(vendor_id=vendor,amount_usd=amount,date=now().date().isoformat(),memo=iid))

def test_evidence_resolution_retries_and_frozen_input():
 g,s,t=setup();event=g.submit(s.session_id,pay(),t)
 assert event.execution=='pending' and event.semantic_evaluation['queue']=='Receiving review'
 old=copy.deepcopy(event.semantic_evaluation)
 append_evidence(g,s.session_id,'INV-7002','Receiving confirms complete delivery for INV-7002; all goods accepted.','verified_receiving')
 assert g.get(s.session_id).events[0].execution=='cancelled'
 assert g.reserved(s)==0
 result=reassess(g,s.session_id,event.event_id)
 assert result.execution=='executed' and result.parent_event_id==event.event_id
 assert reassess(g,s.session_id,event.event_id).result==result.result
 assert len(s.ledger)==1 and event.semantic_evaluation==old
 assert g.store.load()[s.session_id].ledger==s.ledger


def test_supplier_cannot_resolve_and_stale_approval_cannot_execute():
 g,s,t=setup();event=g.submit(s.session_id,pay(),t)
 append_evidence(g,s.session_id,'INV-7002','Receiving confirms complete delivery and accepted.','supplier')
 with pytest.raises(ValueError):g.resolve(s.session_id,event.event_id,'approve')
 assert s.events[0].execution=='cancelled'
 assert reassess(g,s.session_id,event.event_id).execution=='pending'
 assert not s.ledger


def test_evidence_failure_rolls_back_cancellation(monkeypatch):
 g,s,t=setup();g.submit(s.session_id,pay(),t);before=s.model_dump_json()
 def fail(*args):raise OSError('disk')
 monkeypatch.setattr(g.store,'save',fail)
 with pytest.raises(OSError):append_evidence(g,s.session_id,'INV-7002','All accepted','verified_receiving')
 assert s.model_dump_json()==before


def test_no_cross_invoice_supersession_and_no_agent_evidence_access():
 g,s,t=setup()
 with pytest.raises(ValueError):append_evidence(g,s.session_id,'INV-7002','All accepted','verified_receiving','EVD-initial-INV-1042')
 c=TestClient(create_control_app(g,'op'))
 assert c.post(f'/api/sessions/{s.session_id}/invoices/INV-7002/evidence',json={},headers={'Authorization':'Bearer '+t}).status_code==401


@pytest.mark.parametrize('revoked',[True,False])
def test_reassessment_preserves_authority(revoked):
 from datetime import timedelta
 g,s,t=setup();e=g.submit(s.session_id,pay(),t)
 append_evidence(g,s.session_id,'INV-7002','Complete delivery confirmed and accepted','verified_receiving')
 if revoked:g.revoke(s.session_id)
 else:s.mandate.expires_at=now()-timedelta(seconds=1)
 assert reassess(g,s.session_id,e.event_id).verdict=='deny'
 assert not s.ledger


def test_replay_is_historical_and_never_mutates():
 g,s,t=setup();g.submit(s.session_id,pay('INV-1042','V-ACME',4200),t);before=s.model_dump_json()
 result=run_replay(g,s,dict(mode='threshold',review_at=0.1,deny_above=0.9),lambda key:s.policy)
 assert result['rows'][0]['replay']=='allow' and result['model_calls']==0
 assert s.model_dump_json()==before
 result=run_replay(g,s,dict(mode='policy'),lambda key:s.policy)
 assert result['side_effect_free'] and s.model_dump_json()==before


def test_changed_questions_require_fresh_replay():
 g,s,t=setup();g.submit(s.session_id,pay(),t);p=copy.deepcopy(s.policy);p['questions'][0]['instructions']='Different question'
 with pytest.raises(ValueError):run_replay(g,s,dict(mode='threshold'),lambda key:p)


def test_policy_versions_do_not_change_existing_mandates():
 g,s,t=setup();p=copy.deepcopy(s.policy);p['review_at']=0.2;v=validate_policy(p)
 assert v['id']!=s.policy['id'] and s.policy['review_at']==0.35


def test_full_adapter_preserves_fractional_score_and_distributions():
 _,record=evaluate(None,{'evidence':[]},PRESETS[0],True)
 response={k:record[k] for k in ('model','answers','usage')}
 response['answers']['delivery_confirmation'].update(score=1.43,confidence=0.35,probabilities={'0':0.,'1':0.57,'2':0.43,'3':0.})
 client=JevClient('jev-1.14.0',0,transport=httpx.MockTransport(lambda req:httpx.Response(200,json=response)))
 result=client.evaluate_full({},QUESTIONS)
 assert result['answers']['delivery_confirmation']['score']==1.43
 assert result['answers']['exception_category']['probabilities']


@pytest.mark.parametrize('mutation',['missing','label','sum','nan','type'])
def test_malformed_full_answers_rejected(mutation):
 _,record=evaluate(None,{'evidence':[]},PRESETS[0],True);data={k:record[k] for k in ('model','answers','usage')}
 if mutation=='missing':data['answers'].pop('instruction_override')
 if mutation=='label':data['answers']['exception_category']['choice']='unknown'
 if mutation=='sum':data['answers']['delivery_confirmation']['probabilities']['1']=0.9
 if mutation=='nan':data['answers']['instruction_override']['noul']='NaN'
 if mutation=='type':data['answers']['instruction_override']['type']='choice'
 c=JevClient('jev-1.14.0',0,transport=httpx.MockTransport(lambda req:httpx.Response(200,json=data)))
 with pytest.raises(JevError):c.evaluate_full({},QUESTIONS)


def test_oversized_evidence_holds_instead_of_clipping():
 class Client:
  def evaluate_full(self,*args):raise AssertionError('No API call expected')
 with pytest.raises(ValueError):evaluate(Client(),{'evidence':['x'*25000]},PRESETS[0])


def test_api_showcase_and_restart_reassessment():
 g=AuthorizationGateway();app=create_control_app(g,'op');c=TestClient(app);h={'Authorization':'Bearer op'}
 created=c.post('/api/sessions',headers=h,json={}).json();sid=created['session']['session_id']
 event=g.submit(sid,pay(),created['agent_token'])
 g2=AuthorizationGateway(store=g.store);c=TestClient(create_control_app(g2,'op'))
 assert c.post(f'/api/sessions/{sid}/invoices/INV-7002/evidence',headers=h,json={'body':'Complete delivery confirmed and accepted','source_type':'verified_receiving'}).status_code==200
 result=c.post(f'/api/sessions/{sid}/events/{event.event_id}/reassess',headers=h)
 assert result.json()['execution']=='executed'
 assert c.post('/api/replays',headers=h,json={'session_id':sid,'mode':'threshold'}).json()['side_effect_free']


def test_concurrent_reassessment_executes_once():
 from concurrent.futures import ThreadPoolExecutor
 g,s,t=setup();e=g.submit(s.session_id,pay(),t)
 append_evidence(g,s.session_id,'INV-7002','Complete delivery confirmed and accepted','verified_receiving')
 with ThreadPoolExecutor(max_workers=4) as pool:
  results=list(pool.map(lambda _:reassess(g,s.session_id,e.event_id),range(4)))
 assert len({r.event_id for r in results})==1 and len(s.ledger)==1


def test_classifier_outage_is_held_with_no_execution():
 class Broken:
  model='jev-1.14.0'
  def evaluate_full(self,*args):raise OSError('Unavailable')
 g=AuthorizationGateway(SemanticReviewer('jev',client=Broken()));s,t=g.create(policy=PRESETS[0])
 result=g.submit(s.session_id,pay('INV-1042','V-ACME',4200),t)
 assert result.execution=='pending' and not s.ledger


def test_hard_denial_skips_semantic_evaluation():
 g,s,t=setup();e=g.submit(s.session_id,pay(amount=22400),t)
 assert e.verdict=='deny' and e.semantic_evaluation is None and not s.ledger


def test_policy_editor_persists_version_and_leaves_existing_mandate_pinned():
 g,s,t=setup();c=TestClient(create_control_app(g,'op'));h={'Authorization':'Bearer op'}
 draft=copy.deepcopy(s.policy);draft['name']='Review experiment';draft['questions'][0]['instructions']+=' Judge the proposed action only.'
 result=c.post('/api/policies',headers=h,json=draft)
 assert result.status_code==200 and result.json()['id']!=s.policy['id']
 created=c.post('/api/sessions',headers=h,json={'policy_version_id':result.json()['id']}).json()['session']
 assert created['policy']['id']==result.json()['id'] and s.policy['name']!='Review experiment'
