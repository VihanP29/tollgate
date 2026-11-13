"""Optional PostgreSQL acceptance using independent processes/connections.

Set TOLLGATE_TEST_POSTGRES_URL to a disposable database. Every test owns a random
schema; only that schema is dropped. No process-shared Python financial lock exists.
"""
import multiprocessing as mp
import os
import time
import uuid
from datetime import date
import pytest
from sqlalchemy import create_engine, text, select, func
from sqlalchemy.engine import make_url
from tollgate.auth import Principal
from tollgate.decision_context import ProposalV1, Payment, utcnow
from tollgate.jobs.worker import Worker
from tollgate.services.platform import Platform, Conflict
from tollgate.settings import Settings
from tollgate.storage import schema as s
from tollgate.storage.database import Database, row, change
from tollgate.storage.migrate import upgrade

URL = os.environ.get('TOLLGATE_TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Set TOLLGATE_TEST_POSTGRES_URL for independent PostgreSQL tests')


def _platform(url):
    return Platform(Database(url), Settings(database_url=url), 'op')


def _child_submit(url, secret, wid, mid, request_id, iid, amount, vendor, start, output):
    try:
        p = _platform(url)
        agent = p.identity.authenticate(secret)
        start.wait(10)
        result = p.submit(agent, wid, mid, ProposalV1(request_id=request_id,
            action=Payment(invoice_id=iid, amount_minor=amount, beneficiary_ref=vendor, scheduled_date=utcnow().date())))
        for _ in range(20):
            if not Worker(p).tick():
                break
        output.put(('ok',result['id']))
        p.db.dispose()
    except Exception as exc:
        output.put(('error',type(exc).__name__))


def _crash_execute(url):
    class Crash:
        safe_retry = True
        def dispatch(self, *args): os._exit(17)
        def lookup(self,*args): raise AssertionError()
    Worker(_platform(url), Crash()).tick()


def _child_resolve(url, wid, rid, start, output):
    try:
        p = _platform(url); start.wait(10)
        p.resolve(Principal('local-owner'),wid,rid,'approve',1)
        output.put('approved')
    except Conflict:
        output.put('stale')


def _child_evidence(url, wid, start, output):
    p=_platform(url);start.wait(10)
    p.append_evidence(Principal('local-owner'),wid,'INV-1042','Receiving correction; all goods accepted.','verified_receiving')
    output.put('evidence')


@pytest.fixture
def platform():
    schema='tollgate_test_'+uuid.uuid4().hex
    admin=create_engine(URL)
    with admin.begin() as c: c.execute(text(f'CREATE SCHEMA "{schema}"'))
    url=make_url(URL).update_query_dict({'options':'-csearch_path='+schema}).render_as_string(hide_password=False)
    upgrade(url)
    p=_platform(url)
    try:
        yield p
    finally:
        p.db.dispose()
        with admin.begin() as c: c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def setup(p, budget=6000000, auto=1000000):
    actor=Principal('local-owner');wid=p.provision(actor,'workspace')['id']
    mid=p.create_mandate(actor,wid,['INV-1042','INV-7002'],budget,auto)['id']
    cred=p.issue_credential(actor,wid,mid)
    return actor,wid,mid,cred


def race(p,wid,calls):
    context=mp.get_context('spawn');start=context.Event();output=context.Queue()
    processes=[context.Process(target=_child_submit,args=(p.settings.database_url,*args,start,output)) for args in calls]
    for child in processes:child.start()
    start.set()
    results=[output.get(timeout=30) for _ in processes]
    for child in processes:
        child.join(15);assert child.exitcode==0
    assert all(r[0]=='ok' for r in results),results
    # A process may have observed an empty queue before another committed its next job.
    for _ in range(30):
        if not Worker(p).tick():break
    return results


def test_postgres_cross_mandate_invoice_race(platform):
    p=platform;owner,wid,m1,c1=setup(p)
    m2=p.create_mandate(owner,wid,['INV-1042'],6000000,1000000)['id'];c2=p.issue_credential(owner,wid,m2)
    calls=[(c['agent_token'],wid,m,'race-'+str(i),'INV-1042',420000,'V-ACME') for i,(m,c) in enumerate([(m1,c1),(m2,c2),(m1,c1),(m2,c2)])]
    race(p,wid,calls)
    with p.db.transaction() as c:
        assert c.execute(select(func.count()).select_from(s.receipts).where(s.receipts.c.workspace_id==wid)).scalar_one()==1
        mandates=c.execute(select(s.mandates).where(s.mandates.c.workspace_id==wid)).mappings().all()
        assert sum(m['spent_minor'] for m in mandates)==420000
        assert sum(m['reserved_minor'] for m in mandates)==0


def test_postgres_budget_race(platform):
    p=platform;owner,wid,mid,cred=setup(p,500000,500000)
    p.append_evidence(owner,wid,'INV-7002','Complete delivery confirmed and accepted for INV-7002.','verified_receiving')
    race(p,wid,[(cred['agent_token'],wid,mid,'a','INV-1042',420000,'V-ACME'),
                (cred['agent_token'],wid,mid,'b','INV-7002',224000,'V-HOOLI')])
    with p.db.transaction() as c:
        mandate=row(c,s.mandates,wid,mid)
        assert mandate['spent_minor'] in {420000,224000}
        assert mandate['spent_minor']+mandate['reserved_minor']<=500000
        assert c.execute(select(func.count()).select_from(s.receipts).where(s.receipts.c.workspace_id==wid)).scalar_one()==1


def test_postgres_idempotent_submission_and_workers(platform):
    p=platform;owner,wid,mid,cred=setup(p)
    results=race(p,wid,[(cred['agent_token'],wid,mid,'same','INV-1042',420000,'V-ACME')]*4)
    assert len({r[1] for r in results})==1
    with p.db.transaction() as c:
        assert c.execute(select(func.count()).select_from(s.proposals).where(s.proposals.c.workspace_id==wid)).scalar_one()==1
        assert c.execute(select(func.count()).select_from(s.receipts).where(s.receipts.c.workspace_id==wid)).scalar_one()==1


def test_postgres_approval_evidence_race(platform):
    p=platform;owner,wid,mid,cred=setup(p,auto=0)
    agent=p.identity.authenticate(cred['agent_token'])
    result=p.submit(agent,wid,mid,ProposalV1(request_id='review',action=Payment(invoice_id='INV-1042',amount_minor=420000,beneficiary_ref='V-ACME',scheduled_date=utcnow().date())))
    Worker(p).tick()
    review=p.list_resources(owner,wid,s.reviews)[0]
    ctx=mp.get_context('spawn');start=ctx.Event();output=ctx.Queue()
    children=[ctx.Process(target=_child_resolve,args=(p.settings.database_url,wid,review['id'],start,output)),
              ctx.Process(target=_child_evidence,args=(p.settings.database_url,wid,start,output))]
    for child in children:child.start()
    start.set()
    results=[output.get(timeout=30) for _ in children]
    for child in children:child.join(10);assert child.exitcode==0
    for _ in range(5):Worker(p).tick()
    assert p.proposal(agent,wid,result['id'])['state']=='cancelled'
    with p.db.transaction() as c:
        assert row(c,s.mandates,wid,mid)['reserved_minor']==0
        assert c.execute(select(func.count()).select_from(s.receipts).where(s.receipts.c.workspace_id==wid)).scalar_one()==0


def test_postgres_crash_after_dispatch_boundary(platform):
    p=platform;owner,wid,mid,cred=setup(p)
    result=p.submit(p.identity.authenticate(cred['agent_token']),wid,mid,ProposalV1(request_id='crash',action=Payment(invoice_id='INV-1042',amount_minor=420000,beneficiary_ref='V-ACME',scheduled_date=utcnow().date())))
    Worker(p).tick()
    ctx=mp.get_context('spawn');child=ctx.Process(target=_crash_execute,args=(p.settings.database_url,));child.start();child.join(15)
    assert child.exitcode==17
    with p.db.transaction(wid) as c:
        intent=c.execute(select(s.intents).where(s.intents.c.workspace_id==wid)).mappings().one()
        assert intent['state']=='dispatching' and row(c,s.mandates,wid,mid)['reserved_minor']==420000
        key=intent['provider_key']
        c.execute(s.jobs.update().where(s.jobs.c.workspace_id==wid,s.jobs.c.state=='running').values(lease_until=time.time()-1))
    for _ in range(5):Worker(p).tick()
    assert p.proposal(owner,wid,result['id'])['state']=='confirmed'
    with p.db.transaction() as c:
        assert row(c,s.intents,wid,intent['id'])['provider_key']==key
        assert c.execute(select(func.count()).select_from(s.receipts).where(s.receipts.c.workspace_id==wid)).scalar_one()==1


def test_postgres_lab_arms_and_frozen_policy_fairness(platform):
    from tollgate.evaluation.lab import create_experiment, experiment_result, save_policy
    import copy
    p=platform;owner,wid,mid,cred=setup(p)
    original=p.list_resources(owner,wid,s.policies)[0]['payload']
    candidate=copy.deepcopy(original);candidate['name']='All payments require approval';candidate['financial']['auto_limit_cents']=0
    version=save_policy(p,owner,wid,candidate)
    e=create_experiment(p,owner,wid,'synthetic-v1','development',[original['id'],version['id']])
    for _ in range(10):
        if not Worker(p).tick():break
    result=experiment_result(p,owner,wid,e['id'])
    assert result['state']=='completed' and len(result['rows'])==result['manifest']['estimated_rows']==480
    hard=[r for r in result['rows'] if r['arm']=='hard_constraints:'+version['id'] and r['expected']=='allow']
    assert hard and all(r['verdict']=='review' for r in hard)
    with p.db.transaction() as c:
        assert row(c,s.mandates,wid,mid)['spent_minor']==0 and row(c,s.mandates,wid,mid)['reserved_minor']==0


def _child_usage(url,wid,logical,start,output,dispatch=False):
    from tollgate.services.model_usage import ModelUsage
    p=_platform(url)
    settings=Settings(database_url=url,workspace_calls=1,global_calls=1)
    usage=ModelUsage(p.db,settings)
    start.wait(10)
    try:
        request=usage.reserve(wid,logical,1,resource_kind='workspace',resource_id=wid,pool='operational',backend='test-provider',
            model='test-model',request={'fixture':'usage-race'},allowance_tokens=100,timeout_s=10)
        launched=usage.dispatch(wid,request['id']) if dispatch else False
        output.put(('reserved',request['id'],launched))
    except Exception as exc:
        output.put((type(exc).__name__,None,False))
    finally:p.db.dispose()


def test_postgres_usage_quotas_across_workspaces_and_same_attempt_delivery(platform):
    p=platform;owner,wid,mid,cred=setup(p)
    other=p.provision(Principal('other-owner'),'workspace')['id']
    ctx=mp.get_context('spawn');start=ctx.Event();output=ctx.Queue()
    children=[ctx.Process(target=_child_usage,args=(p.settings.database_url,w,'claim-'+str(i),start,output))
              for i,w in enumerate([wid,other,wid,other])]
    for child in children:child.start()
    start.set();results=[output.get(timeout=30) for _ in children]
    for child in children:child.join(15);assert child.exitcode==0
    assert sum(r[0]=='reserved' for r in results)==1
    assert sum(r[0]=='QuotaExceeded' for r in results)==3
    with p.db.transaction() as c:
        global_usage=c.execute(select(s.usage).where(s.usage.c.scope=='global')).mappings().one()
        assert global_usage['reserved_calls']==1 and global_usage['reserved_tokens']==100
        request=c.execute(select(s.model_requests)).mappings().one()
    # A fresh race retries the same durable logical attempt; exactly one dispatch marker wins.
    start=ctx.Event();output=ctx.Queue()
    children=[ctx.Process(target=_child_usage,args=(p.settings.database_url,request['workspace_id'],request['logical_key'],start,output,True)) for _ in range(4)]
    for child in children:child.start()
    start.set();results=[output.get(timeout=30) for _ in children]
    for child in children:child.join(15);assert child.exitcode==0
    assert all(r[0]=='reserved' for r in results)
    assert len({r[1] for r in results})==1 and sum(r[2] for r in results)==1
    with p.db.transaction() as c:
        global_usage=c.execute(select(s.usage).where(s.usage.c.scope=='global')).mappings().one()
        assert global_usage['calls']==1 and global_usage['reserved_calls']==0
