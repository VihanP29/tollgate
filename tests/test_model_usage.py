"""Durable model allowance and strict canonical judge contracts; no paid requests."""
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from tollgate.auth import Principal
from tollgate.clients.general_judge import GeneralJudgeClient,GeneralJudgeConfig
from tollgate.evaluation.datasets import build_case
from tollgate.evaluation.general import BudgetedGeneralJudge
from tollgate.evaluation.lab import create_experiment,experiment_result
from tollgate.jobs.worker import Worker
from tollgate.services.model_usage import ModelUsage
from tollgate.services.platform import Platform,Conflict,QuotaExceeded
from tollgate.settings import Settings
from tollgate.storage import schema as s
from tollgate.storage.database import Database,row,change
from tollgate.storage.migrate import upgrade


@pytest.fixture
def context(tmp_path):
    url='sqlite:///'+str(tmp_path/'usage.sqlite');upgrade(url)
    db=Database(url);settings=Settings(database_url=url)
    platform=Platform(db,settings,'op');actor=Principal('local-owner');wid=platform.provision(actor,'usage')['id']
    yield platform,actor,wid,ModelUsage(db,settings)
    db.dispose()


def reserve(context,key='test',attempt=1,**values):
    p,_,wid,usage=context
    args=dict(resource_kind='workspace',resource_id=wid,pool='operational',backend='test-provider',model='fixed-model',
              request={'model':'fixed-model','state':'test'},allowance_tokens=100,timeout_s=10)
    args.update(values)
    return usage.reserve(wid,key,attempt,**args)


def counters(context,scope=None):
    p,_,wid,_=context
    with p.db.transaction() as c:
        return dict(c.execute(select(s.usage).where(s.usage.c.scope==(scope or wid))).mappings().one())


def test_reservation_dispatch_and_idempotent_settlement(context):
    p,_,wid,usage=context
    r=reserve(context);assert reserve(context)['id']==r['id']
    assert counters(context)['calls']==0 and counters(context)['reserved_calls']==1
    assert usage.dispatch(wid,r['id']) and not usage.dispatch(wid,r['id'])
    assert counters(context)['calls']==1 and counters(context)['reserved_calls']==0
    args=dict(response={'availability':'available'},usage={'input_tokens':10,'output_tokens':2,'cache_read_input_tokens':20,'cache_creation_input_tokens':5},resolved_model='fixed-model')
    final=usage.finish(wid,r['id'],**args)
    assert final['state']=='settled' and final['charged_tokens']==37
    assert usage.finish(wid,r['id'],**args)==final
    assert counters(context)['tokens']==37 and counters(context)['reserved_tokens']==0
    with pytest.raises(Conflict):usage.finish(wid,r['id'],**{**args,'usage':{'input_tokens':11,'output_tokens':2}})
    with p.db.transaction() as c:
        assert c.execute(select(func.count()).select_from(s.model_usage_events)).scalar_one()==3
        assert c.execute(select(func.count()).select_from(s.reservations)).scalar_one()==0


def test_changed_identity_or_retry_sequence_conflicts(context):
    r=reserve(context)
    with pytest.raises(Conflict):reserve(context,request={'state':'different'})
    with pytest.raises(Conflict):reserve(context,attempt=2)
    with pytest.raises(Conflict):reserve(context,'new',attempt=2)
    with pytest.raises(ValueError):reserve(context,'new',attempt=True)
    context[3].cancel(context[2],r['id'])
    with pytest.raises(ValueError):reserve(context,'new',attempt=4)
    assert reserve(context,attempt=2)['attempt']==2


def test_unknown_usage_is_charged_until_authoritative_reconciliation(context):
    p,actor,wid,_=context
    limited=ModelUsage(p.db,Settings(database_url=p.settings.database_url,workspace_tokens=150,global_tokens=150))
    ctx=(p,actor,wid,limited);r=reserve(ctx)
    limited.dispatch(wid,r['id']);limited.finish(wid,r['id'],response={'availability':'unavailable'},error_code='provider_unavailable')
    assert counters(context)['tokens']==100 and counters(context)['reserved_tokens']==0
    with pytest.raises(QuotaExceeded):reserve(ctx,'second')
    limited.finish(wid,r['id'],response={'availability':'available'},usage={'input_tokens':5,'output_tokens':5},resolved_model='fixed-model')
    assert counters(context)['tokens']==10
    assert reserve(ctx,'second')['state']=='reserved'


def test_recovery_releases_only_undispatched_allowance(context):
    p,_,wid,usage=context
    cancelled=reserve(context,'before');unknown=reserve(context,'after');usage.dispatch(wid,unknown['id'])
    with p.db.transaction() as c:
        c.execute(s.model_requests.update().values(deadline=time.time()-1))
    assert usage.recover();assert not usage.recover()
    with p.db.transaction() as c:
        assert row(c,s.model_requests,wid,cancelled['id'])['state']=='cancelled'
        assert row(c,s.model_requests,wid,unknown['id'])['state']=='unknown'
    assert counters(context)['calls']==1 and counters(context)['tokens']==100
    assert counters(context)['reserved_calls']==counters(context)['reserved_tokens']==0
    assert not usage.dispatch(wid,unknown['id'])


def test_evaluation_pool_leaves_operational_capacity(context):
    p,a,wid,_=context
    usage=ModelUsage(p.db,Settings(database_url=p.settings.database_url,global_calls=2,workspace_calls=2,
                                 global_tokens=400,workspace_tokens=400,evaluation_usage_fraction=.5))
    ctx=(p,a,wid,usage);r=reserve(ctx,'evaluation',pool='evaluation')
    with pytest.raises(QuotaExceeded):reserve(ctx,'evaluation2',pool='evaluation')
    other=reserve(ctx,'operation')
    assert other['state']=='reserved'
    with pytest.raises(QuotaExceeded):reserve(ctx,'third')
    usage.cancel(wid,r['id']);usage.cancel(wid,other['id'])
    assert counters(context)['reserved_calls']==0


def test_independent_connections_cannot_overreserve(context):
    p,a,wid,_=context
    settings=Settings(database_url=p.settings.database_url,global_calls=1,workspace_calls=1)
    def submit(n):
        usage=ModelUsage(Database(settings.database_url),settings)
        try:
            return reserve((p,a,wid,usage),str(n))['id']
        except QuotaExceeded:return None
        finally:usage.db.dispose()
    with ThreadPoolExecutor(4) as pool:results=list(pool.map(submit,range(4)))
    assert sum(r is not None for r in results)==1
    assert counters(context)['reserved_calls']==1


@pytest.mark.parametrize('bad',[{'input_tokens':True,'output_tokens':1},{'input_tokens':-1,'output_tokens':1},
 {'input_tokens':1.2,'output_tokens':1},{'input_tokens':1,'output_tokens':1,'total_tokens':2},
 {'input_tokens':2**63,'output_tokens':1}])
def test_invalid_usage_cannot_release_reserved_tokens(context,bad):
    _,_,wid,usage=context;r=reserve(context);usage.dispatch(wid,r['id'])
    with pytest.raises(ValueError):usage.finish(wid,r['id'],response={},usage=bad)
    assert counters(context)['reserved_tokens']==100


class FakeSDK:
    def __init__(self,mutate=None):
        self.messages=self;self.calls=[];self.mutate=mutate;self.options=None
    def with_options(self,**kwargs):self.options=kwargs;return self
    def create(self,**kwargs):
        self.calls.append(kwargs)
        response=SimpleNamespace(model=kwargs['model'],stop_reason='end_turn',usage=SimpleNamespace(input_tokens=12,output_tokens=8),
            content=[SimpleNamespace(type='text',text=json.dumps(dict(verdict='allow',reason_codes=['none'],reason='All applicable semantic requirements satisfied.')))])
        return self.mutate(response) if self.mutate else response


def driver(context,sdk=None,allowance=1000):
    sdk=sdk or FakeSDK()
    client=GeneralJudgeClient(GeneralJudgeConfig(model='test-model-pin',allowance_tokens=allowance,max_output_tokens=128),sdk)
    return BudgetedGeneralJudge(client,context[3]),sdk


def run_judge(context,judge,value=None,attempt=1,key='judge'):
    value=value or build_case({'scenario':'routine','variant':0},context[2])
    return judge.evaluate(value,value.unpack('policy'),logical_key=key,attempt=attempt,resource_kind='workspace',resource_id=context[2])


def test_canonical_judge_hides_labels_and_reuses_exact_result(context):
    judge,sdk=driver(context);first=run_judge(context,judge);second=run_judge(context,judge)
    assert first['verdict']=='allow' and first['model_calls']==1 and second['model_calls']==0
    assert second['reused_answers'] and second['provider_latency_ms'] is None and second['usage']=={}
    assert sdk.options=={'max_retries':0,'timeout':10}
    sent=json.loads(sdk.calls[0]['messages'][0]['content'])
    assert 'financial_state' in sent['state'] and 'semantic_rules' in sent
    assert all(key not in json.dumps(sent) for key in ['expected','adjudication','family_id','held_out'])
    assert counters(context)['calls']==1 and counters(context)['tokens']==20


def test_hard_denial_skips_judge_and_usage(context):
    judge,sdk=driver(context)
    result=run_judge(context,judge,build_case({'scenario':'wrong_amount','variant':0},context[2]))
    assert result['verdict']=='deny' and result['model_calls']==0 and not sdk.calls
    assert counters(context)['calls']==counters(context)['reserved_calls']==0


@pytest.mark.parametrize('mutation,code',[
 (lambda r:SimpleNamespace(**{**r.__dict__,'model':'unexpected'}),'model_mismatch'),
 (lambda r:SimpleNamespace(**{**r.__dict__,'stop_reason':'refusal'}),'refusal'),
 (lambda r:SimpleNamespace(**{**r.__dict__,'stop_reason':'max_tokens'}),'truncated_output'),
 (lambda r:SimpleNamespace(**{**r.__dict__,'usage':SimpleNamespace(input_tokens=True,output_tokens=1)}),'invalid_usage'),
 (lambda r:SimpleNamespace(**{**r.__dict__,'content':[SimpleNamespace(type='text',text='{"verdict":"allow","reason_codes":["none"],"reason":"ok","actor":"owner"}')] }),'malformed_output'),
 (lambda r:SimpleNamespace(**{**r.__dict__,'content':[SimpleNamespace(type='text',text='{"verdict":"deny","reason_codes":["conflicting_receiving_evidence"],"reason":"contradictory"}')] }),'malformed_output'),
])
def test_judge_faults_hold_and_charge_attempts(context,mutation,code):
    judge,sdk=driver(context,FakeSDK(mutation));result=run_judge(context,judge)
    assert result['verdict']=='review' and result['error_code']==code and result['model_calls']==1
    assert counters(context)['calls']==1 and counters(context)['reserved_tokens']==0


def test_sdk_error_redaction_and_retry_allowance(context):
    def fail(_):raise TimeoutError('SECRET request evidence and credentials')
    judge,sdk=driver(context,FakeSDK(fail))
    result=run_judge(context,judge)
    assert result['error_code']=='timeout' and counters(context)['tokens']==1000
    again=run_judge(context,judge)
    assert again['model_calls']==0 and len(sdk.calls)==1
    run_judge(context,judge,attempt=2)
    assert len(sdk.calls)==2 and counters(context)['tokens']==2000
    with context[0].db.transaction() as c:
        records=[dict(r) for r in c.execute(select(s.model_requests)).mappings()]
        assert 'SECRET' not in json.dumps(records)


def test_budget_overrun_is_visible_and_cannot_return_allow(context):
    judge,sdk=driver(context,FakeSDK(lambda r:SimpleNamespace(**{**r.__dict__,'usage':SimpleNamespace(input_tokens=200,output_tokens=8)})),allowance=128)
    result=run_judge(context,judge)
    assert result['verdict']=='review' and result['error_code']=='usage_allowance_exceeded'
    assert counters(context)['tokens']==208
    assert run_judge(context,judge)['verdict']=='review'


def test_network_callback_does_not_hold_database_write_lock(context):
    def another_write(response):
        with ThreadPoolExecutor(1) as pool:
            request=pool.submit(reserve,context,'independent').result(timeout=3)
        context[3].cancel(context[2],request['id'])
        return response
    judge,_=driver(context,FakeSDK(another_write))
    assert run_judge(context,judge)['availability']=='available'


def test_lab_general_configuration_is_frozen_and_resumes_without_new_calls(context):
    p,actor,wid,usage=context
    cfg=GeneralJudgeConfig(model='test-model-pin',allowance_tokens=256,max_output_tokens=128)
    sdk=FakeSDK();judge=BudgetedGeneralJudge(GeneralJudgeClient(cfg,sdk),usage)
    policy=p.list_resources(actor,wid,s.policies)[0]['id']
    experiment=create_experiment(p,actor,wid,'synthetic-v1','development',[policy],general_judge_config=cfg)
    assert experiment['manifest']['general_judge_config']==cfg.model_dump(mode='json')
    assert experiment['manifest']['estimated_provider_calls']==55
    Worker(p,general_judge=judge).tick()
    result=experiment_result(p,actor,wid,experiment['id'])
    assert result['state']=='completed' and len(result['rows'])==240
    assert len(sdk.calls)==20  # evaluation sub-budget, rather than the workspace's entire allowance
    assert result['model_usage']['dispatched_attempts']==20
    assert result['model_usage']['charged_tokens']==400
    assert any(r.get('error_code')=='quota_exhausted' for r in result['rows'])
    with p.db.transaction(wid) as c:change(c,s.experiments,wid,experiment['id'],state='running')
    from tollgate.evaluation.lab import Lab,EvaluationRepository
    Lab(EvaluationRepository(p.db),judge).run(wid,experiment['id'],attempt=2)
    assert len(sdk.calls)==20
    with p.db.transaction() as c:
        assert c.execute(select(func.count()).select_from(s.receipts)).scalar_one()==0
        assert c.execute(select(func.count()).select_from(s.reservations)).scalar_one()==0


def test_usage_views_are_scoped_and_agent_cannot_read(context):
    from fastapi.testclient import TestClient
    from tollgate.platform_server import create_app
    p,actor,wid,usage=context;r=reserve(context)
    client=TestClient(create_app(p.settings,p.db,'op'));headers={'Authorization':'Bearer op'}
    view=client.get(f'/api/v1/workspaces/{wid}/model-usage',headers=headers)
    assert view.status_code==200 and view.json()['workspace']['total']['reserved_calls']==1
    detail=client.get(f'/api/v1/workspaces/{wid}/model-usage/{r["id"]}',headers=headers)
    assert detail.status_code==200 and len(detail.json()['events'])==1
    mid=p.create_mandate(actor,wid,['INV-1042'],6000000,1000000)['id']
    secret=p.issue_credential(actor,wid,mid)['agent_token']
    assert client.get(f'/api/v1/workspaces/{wid}/model-usage',headers={'Authorization':'Bearer '+secret}).status_code==404
    other=p.provision(Principal('other'),'other')['id']
    assert client.get(f'/api/v1/workspaces/{other}/model-usage/{r["id"]}',headers=headers).status_code==404


def test_missing_or_different_runtime_adapter_never_falls_back_to_fixtures(context):
    p,actor,wid,usage=context
    cfg=GeneralJudgeConfig(model='test-model-pin',allowance_tokens=256,max_output_tokens=128)
    policy=p.list_resources(actor,wid,s.policies)[0]['id']
    experiment=create_experiment(p,actor,wid,'synthetic-v1','development',[policy],general_judge_config=cfg)
    other_sdk=FakeSDK();other_cfg=GeneralJudgeConfig(model='different-pin',allowance_tokens=256,max_output_tokens=128)
    Worker(p,general_judge=BudgetedGeneralJudge(GeneralJudgeClient(other_cfg,other_sdk),usage)).tick()
    result=experiment_result(p,actor,wid,experiment['id'])
    rows=[r for r in result['rows'] if r['arm'].startswith('general_llm:') and r['hard_pass']]
    assert rows and all(r['availability']=='unavailable' for r in rows)
    assert not other_sdk.calls and result['model_usage']['dispatched_attempts']==0


def test_reset_stops_experiment_after_inflight_usage_is_recorded(context):
    p,actor,wid,usage=context
    cfg=GeneralJudgeConfig(model='test-model-pin',allowance_tokens=256,max_output_tokens=128)
    def reset(response):
        revision=p.workspaces(actor)[0]['revision']
        p.reset_synthetic(actor,wid,revision)
        return response
    sdk=FakeSDK(reset);judge=BudgetedGeneralJudge(GeneralJudgeClient(cfg,sdk),usage)
    policy=p.list_resources(actor,wid,s.policies)[0]['id']
    experiment=create_experiment(p,actor,wid,'synthetic-v1','development',[policy],general_judge_config=cfg)
    Worker(p,general_judge=judge).tick()
    result=experiment_result(p,actor,wid,experiment['id'])
    assert result['state']=='cancelled' and len(sdk.calls)==1
    assert result['model_usage']['dispatched_attempts']==1
    assert counters(context)['calls']==1 and counters(context)['tokens']==20
    assert len(result['rows'])<result['manifest']['estimated_rows']


def test_allowance_and_model_configuration_reject_unsafe_defaults():
    with pytest.raises(ValueError):GeneralJudgeConfig(model='jev-latest')
    with pytest.raises(ValueError):GeneralJudgeConfig(model='fixed-model',max_output_tokens=1024,allowance_tokens=128)


def test_usage_migration_roundtrip_and_history_downgrade_guard(context):
    from alembic import command
    from alembic.config import Config
    p,_,wid,usage=context
    config=Config('alembic.ini');config.attributes['database_url']=p.settings.database_url
    with p.db.transaction() as c:
        c.execute(s.usage.update().where(s.usage.c.scope.in_(['global',wid])).values(calls=5,tokens=99))
    command.downgrade(config,'0003_lab_arms')
    assert not p.db.ready()
    upgrade(p.settings.database_url)
    assert p.db.ready()
    assert counters(context)['calls']==5 and counters(context)['tokens']==99
    assert counters(context,'global')['calls']==5 and counters(context,'global')['tokens']==99
    reserve(context)
    with pytest.raises(RuntimeError):command.downgrade(config,'0003_lab_arms')
    assert p.db.ready()
    with p.db.transaction() as c:
        assert c.execute(select(func.count()).select_from(s.model_requests)).scalar_one()==1


def test_reply_persisted_before_worker_crash_is_not_billed_again(context):
    judge,sdk=driver(context)
    original=run_judge(context,judge)
    resumed=run_judge(context,judge,attempt=2)
    resumed_again=run_judge(context,judge,attempt=3)
    assert len(sdk.calls)==1 and counters(context)['calls']==1
    assert original['usage_request_id']==resumed['usage_request_id']==resumed_again['usage_request_id']
    assert resumed['reused_answers'] and resumed_again['model_calls']==0


def test_cached_semantic_allow_is_recomposed_with_stricter_hard_checks(context):
    judge,sdk=driver(context)
    value=build_case({'scenario':'routine','variant':0},context[2])
    assert run_judge(context,judge,value)['verdict']=='allow'
    policy=value.unpack('policy');policy['financial']['auto_limit_cents']=0
    result=judge.evaluate(value,policy,logical_key='judge',attempt=1,resource_kind='workspace',resource_id=context[2])
    assert result['reused_answers'] and result['verdict']=='review' and len(sdk.calls)==1
    assert any(c['id']=='human_limit' and c['outcome']=='review' for c in result['checks'])


def test_lab_restart_reuses_saved_reply_and_reaches_uncalled_cases(context):
    from tollgate.evaluation.lab import Lab,EvaluationRepository
    p,actor,wid,usage=context
    judge,sdk=driver(context)
    policy=p.list_resources(actor,wid,s.policies)[0]['id']
    experiment=create_experiment(p,actor,wid,'synthetic-v1','development',[policy],general_judge_config=judge.config)

    class CrashBeforeRow(EvaluationRepository):
        def put_result(self,table,workspace_id,values):
            if values['arm'].startswith('general_llm:'):
                raise RuntimeError('worker stopped after reply persisted')
            return super().put_result(table,workspace_id,values)

    with pytest.raises(RuntimeError):Lab(CrashBeforeRow(p.db),judge).run(wid,experiment['id'])
    assert len(sdk.calls)==1
    Lab(EvaluationRepository(p.db),judge).run(wid,experiment['id'],attempt=2)
    result=experiment_result(p,actor,wid,experiment['id'])
    assert result['state']=='completed' and len(result['rows'])==240
    assert len(sdk.calls)==20 and result['model_usage']['dispatched_attempts']==20
    assert any(r['reused_answers'] for r in result['rows'] if r['arm'].startswith('general_llm:'))
    assert not any(r.get('error_code')=='request_identity_conflict' for r in result['rows'])
    with p.db.transaction() as c:
        attempts=c.execute(select(s.model_requests)).mappings().all()
        assert len(attempts)==20 and all(a['attempt']==1 for a in attempts)


def test_cancel_between_reservation_and_dispatch_releases_allowance(context,monkeypatch):
    p,actor,wid,usage=context
    judge,sdk=driver(context)
    policy=p.list_resources(actor,wid,s.policies)[0]['id']
    experiment=create_experiment(p,actor,wid,'synthetic-v1','development',[policy],general_judge_config=judge.config)
    dispatch=usage.dispatch

    def cancel_then_dispatch(workspace_id,request_id):
        with p.db.transaction(wid) as c:
            change(c,s.experiments,wid,experiment['id'],state='cancelled')
        return dispatch(workspace_id,request_id)

    monkeypatch.setattr(usage,'dispatch',cancel_then_dispatch)
    value=build_case({'scenario':'routine','variant':0},wid)
    result=judge.evaluate(value,value.unpack('policy'),logical_key='cancel-before-dispatch',attempt=1,
        resource_kind='experiment',resource_id=experiment['id'])
    assert result['error_code']=='model_attempt_cancelled' and result['model_calls']==0 and not sdk.calls
    assert counters(context)['calls']==counters(context)['tokens']==0
    assert counters(context)['reserved_calls']==counters(context)['reserved_tokens']==0
    assert usage.get(wid,result['usage_request_id'])['error_code']=='resource_cancelled'
