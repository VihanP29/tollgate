import copy
import pytest
from fastapi.testclient import TestClient
from tollgate.authorization import AuthorizationGateway, AuditStore, Proposal, now
from tollgate.demo_server import create_control_app
from tollgate.policy_config import migrate,validate,request
from tollgate.semantic_policy import PRESETS,compose

H={'Authorization':'Bearer op'}
def setup(path=':memory:'):
 g=AuthorizationGateway(store=AuditStore(path));c=TestClient(create_control_app(g,'op'));return g,c

def clone(c):
 r=c.post('/api/policy-drafts',json={},headers=H);assert r.status_code==200,r.text;return r.json()
def save(c,d):
 r=c.patch('/api/policy-drafts/'+d['id'],json={'revision':d['revision'],'bundle':d['bundle'],'confirm_rubric':True},headers=H);assert r.status_code==200,r.text;return r.json()
def compare(c,d,**kwargs):
 r=c.post('/api/policy-comparisons',json={'draft_id':d['id'],'revision':d['revision'],**kwargs},headers=H);assert r.status_code==200,r.text;return r.json()

def test_full_activation_pinning_restart(tmp_path):
 path=str(tmp_path/'db.sqlite');g,c=setup(path);old=c.post('/api/sessions',headers=H,json={}).json()['session'];d=clone(c)
 d['bundle']['financial']['auto_limit_cents']=0;d=save(c,d);before=copy.deepcopy(g.store.load());result=compare(c,d)
 assert result['passed'],result
 assert g.store.load()==before
 version=c.post('/api/policy-versions',headers=H,json={'draft_id':d['id'],'revision':d['revision']}).json()
 active=c.get('/api/policy-studio',headers=H).json()['active_version']
 r=c.post('/api/policy-activations',headers=H,json={'version_id':version['id'],'comparison_id':result['id'],'expected_active':active});assert r.status_code==200,r.text
 new=c.post('/api/sessions',headers=H,json={});assert new.status_code==200,new.text
 assert new.json()['session']['mandate']['auto_limit_cents']==0
 assert g.get(old['session_id']).policy['id']==old['policy']['id']
 g2,c2=setup(path);assert c2.get('/api/policy-studio',headers=H).json()['active_version']==version['id']
 assert c2.get('/api/policy-comparisons/'+result['id'],headers=H).json()==result
 assert c.post('/api/policy-activations',headers=H,json={'version_id':version['id'],'comparison_id':result['id'],'expected_active':active}).status_code==409

@pytest.mark.parametrize('mutate',[
 lambda b:b.update(unknown=True),lambda b:b['financial'].update(auto_limit_cents=-1),lambda b:b['financial'].update(budget_cents=True),
 lambda b:b['rules']['instruction_override'].update(review_at=.99,deny_above=.1),lambda b:b['routing'].pop('none'),
 lambda b:b.update(acceptable_levels=[99]),lambda b:b['questions'][3].update(criteria=['one']),lambda b:b['questions'][0].update(criteria={'true':'yes'}),
])
def test_contract_validation(mutate):
 b=migrate(PRESETS[0]);mutate(b)
 with pytest.raises(ValueError):validate(b)

def test_exact_guidance_noul_contract_and_polarity():
 b=migrate(PRESETS[0]);b['shared_guidance']='Shared guidance unique';b['questions'][0]['criteria']={'true':'required','false':'missing'}
 compiled=request(validate(b),{})
 assert compiled['questions']['instruction_override']['criteria']=={'true':'required','false':'missing'}
 assert all(q['instructions'].count('Shared guidance unique')==1 for q in compiled['questions'].values())
 a={q['id']:{'noul':.01} for q in b['questions'][:3]};a.update(delivery_confirmation={'score':3,'confidence':1,'probabilities':{'0':0,'1':0,'2':0,'3':1}},exception_category={'choice':'none','confidence':1})
 b['rules']['instruction_override']['true_means']='requirement'
 assert compose(a,b)[0][0]['outcome']=='fail'


def test_offline_prompt_edits_not_fake_evaluated_and_activation_stale():
 g,c=setup();d=clone(c);result=compare(c,d);assert result['passed']
 d['bundle']['shared_guidance']='New policy';d=save(c,d);result2=compare(c,d)
 assert not result2['complete'] and all('live Jev' in r['error'] for r in result2['rows'])
 version=c.post('/api/policy-versions',headers=H,json={'draft_id':d['id'],'revision':d['revision']}).json()
 r=c.post('/api/policy-activations',headers=H,json={'version_id':version['id'],'comparison_id':result['id'],'expected_active':c.get('/api/policy-studio',headers=H).json()['active_version']})
 assert r.status_code==422
 assert c.patch('/api/policy-drafts/'+d['id'],headers=H,json={'revision':1,'bundle':d['bundle']}).status_code==409


def test_historical_reuse_and_financial_snapshot():
 g,c=setup();r=c.post('/api/sessions',headers=H,json={}).json();s=g.get(r['session']['session_id']);p=Proposal(tool='schedule_payment',args={'vendor_id':'V-HOOLI','amount_usd':2240,'date':now().date().isoformat(),'memo':'INV-7002'})
 e=g.submit(s.session_id,p,r['agent_token']);assert e.financial_snapshot
 d=clone(c);d['bundle']['require_confirmation']=False;d=save(c,d);before=s.model_dump_json()
 result=compare(c,d,mode='reuse',session_id=s.session_id)
 assert result['complete'] and result['model_calls']==0 and result['rows'][0]['draft']['verdict']=='allow'
 assert s.model_dump_json()==before
 d['bundle']['shared_guidance']='changed';d=save(c,d)
 assert c.post('/api/policy-comparisons',headers=H,json={'draft_id':d['id'],'revision':d['revision'],'mode':'reuse','session_id':s.session_id}).status_code==422


def test_auditor_server_permissions(monkeypatch):
 monkeypatch.setenv('TOLLGATE_AUDITOR_TOKEN','audit');g,c=setup();h={'Authorization':'Bearer audit'}
 assert c.get('/api/policy-studio',headers=h).json()['can_edit'] is False
 assert c.get('/api/policy-scenarios',headers=h).status_code==200
 for path in ['/api/policy-drafts','/api/policy-versions','/api/policy-activations','/api/policy-comparisons']:
  assert c.post(path,headers=h,json={}).status_code==401


def test_stricter_mandate_limit_and_rubric_confirmation():
 g,c=setup();d=clone(c);d['bundle']['financial']['budget_cents']=100000;d['bundle']['financial']['auto_limit_cents']=0;d=save(c,d)
 assert compare(c,d)['passed']
 assert c.post('/api/sessions',headers=H,json={'policy_version_id':d['bundle']['id'],'budget_usd':60000}).status_code==422
 d['bundle']['questions'][3]['criteria'][0]='Changed rubric'
 assert c.patch('/api/policy-drafts/'+d['id'],headers=H,json={'revision':d['revision'],'bundle':d['bundle']}).status_code==422
