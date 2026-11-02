"""Policy Studio lifecycle and side-effect-free experiments."""
import copy
import json
import uuid
from datetime import datetime
from fastapi import Depends, Header, HTTPException
from fastapi.responses import FileResponse
from tollgate.policy_config import migrate, validate, request, COMPILER
from tollgate.semantic_policy import PRESETS, hashed, evaluate, compose
from tollgate.authorization import AuthorizationGateway, ControlSession, Proposal, now, ROOT


def install(app,gateway,operator,token):
    import hmac, os
    auditor_token=os.environ.get('TOLLGATE_AUDITOR_TOKEN','')
    def reader(authorization: str=Header(default='')):
        value=authorization.removeprefix('Bearer ')
        if not value or not (hmac.compare_digest(value,token) or (auditor_token and hmac.compare_digest(value,auditor_token))): raise HTTPException(401,'Operator or auditor credential required')
        return 'local_operator' if hmac.compare_digest(value,token) else 'local_auditor'
    def put(key,kind,value):
        if kind=='bundle':
            old=gateway.store.db.execute('SELECT payload FROM showcase_objects WHERE id=?',(key,)).fetchone()
            if old and json.loads(old[0])!=value: raise ValueError('Immutable version ID collision')
        gateway.store.db.execute('INSERT OR REPLACE INTO showcase_objects VALUES (?,?,?)',(key,kind,json.dumps(value)))
    def get(key,kind):
        row=gateway.store.db.execute('SELECT payload FROM showcase_objects WHERE id=? AND kind=?',(key,kind)).fetchone()
        if not row: raise ValueError('Unknown '+kind)
        return json.loads(row[0])
    def listing(kind):
        return [json.loads(r[0]) for r in gateway.store.db.execute('SELECT payload FROM showcase_objects WHERE kind=?',(kind,))]
    def version(key): return get(key,'bundle')
    def active(): return get('studio-active','active')['version_id']
    with gateway.lock,gateway.store.db:
        for p in PRESETS:
            b=migrate(p);put(b['id'],'bundle',b)
        if not gateway.store.db.execute("SELECT 1 FROM showcase_objects WHERE id='studio-active'").fetchone(): put('studio-active','active',{'version_id':migrate(PRESETS[0])['id']})
    app.state.studio_version=version; app.state.studio_active=active
    app.state.studio_eligible=lambda key: key in {migrate(p)["id"] for p in PRESETS} or any(a["version_id"]==key for a in listing("activation"))
    def route_error(fn):
        from functools import wraps
        @wraps(fn)
        def call(*a,**kw):
            try:
                with gateway.lock: return fn(*a,**kw)
            except (ValueError,KeyError,TypeError,IndexError) as exc: raise HTTPException(422,str(exc))
        return call
    @app.get('/studio')
    def page(): return FileResponse(ROOT/'dashboard/studio.html')
    @app.get('/studio.js')
    def script(): return FileResponse(ROOT/'dashboard/studio.js',media_type='text/javascript')
    @app.get('/studio.css')
    def styles(): return FileResponse(ROOT/'dashboard/studio.css',media_type='text/css')
    @app.get('/api/policy-studio')
    @route_error
    def overview(actor=Depends(reader)):
        return dict(active_version=active(),versions=listing('bundle'),drafts=listing('draft'),activations=listing('activation'),actor=actor,can_edit=actor=='local_operator',compiler_version=COMPILER,backend=gateway.reviewer.backend,version_metadata=listing('version_metadata'),comparisons=[{k:c[k] for k in ('id','draft_id','baseline_id','created_at','passed','model_calls')} for c in listing('comparison')])
    @app.post('/api/policy-preview',dependencies=[Depends(reader)])
    @route_error
    def compiled_preview(body:dict):
        b=validate(body['bundle'])
        return dict(valid=True,bundle_hash=b['hash'],request=request(b,body.get('state',{})),compiler_version=COMPILER)
    @app.post('/api/policy-drafts',dependencies=[Depends(operator)])
    @route_error
    def clone(body:dict):
        b=version(body.get('version_id',active()))
        d=dict(id='DFT-'+uuid.uuid4().hex[:12],parent=b['id'],revision=1,bundle=b,actor='local_operator',updated_at=now().isoformat())
        with gateway.store.db: put(d['id'],'draft',d)
        return d
    @app.patch('/api/policy-drafts/{key}',dependencies=[Depends(operator)])
    @route_error
    def update(key:str,body:dict):
        d=get(key,'draft')
        if body['revision']!=d['revision']: raise HTTPException(409,'Draft changed; reload before saving')
        b=validate(body['bundle'])
        # Rubric thresholds must be explicitly reconfirmed after edits.
        if b['questions'][3]['criteria']!=d['bundle']['questions'][3]['criteria'] and not body.get('confirm_rubric'): raise ValueError('Reconfirm score thresholds and acceptable levels after changing the rubric')
        d.update(bundle=b,revision=d['revision']+1,updated_at=now().isoformat())
        with gateway.store.db: put(key,'draft',d)
        return d
    @app.post('/api/policy-drafts/{key}/validate',dependencies=[Depends(operator)])
    @route_error
    def preview(key:str,body:dict):
        b=validate(body.get('bundle',get(key,'draft')['bundle']))
        return dict(valid=True,bundle=b,request=request(b,body.get('state',{'preview':'Select a scenario for exact state'})),bundle_hash=b['hash'],compiler_version=COMPILER)
    def suite():
        isolated=AuthorizationGateway()
        session,_=isolated.create(policy=PRESETS[0]); iid='INV-7002';vendor=session.invoices[iid]['vendor_id']
        p=Proposal(request_id='scenario',tool='schedule_payment',args=dict(vendor_id=vendor,amount_usd=2240,date=now().date().isoformat(),memo=iid))
        rows=[]
        for key,text,source in [('complete','complete delivery confirmed and accepted.','verified_receiving'),('missing','','verified_receiving'),('supplier','complete delivery confirmed and accepted.','supplier'),('dispute','delivery dispute remains unresolved; eight cartons missing.','verified_receiving')]:
            s=session.model_copy(deep=True);s.evidence[iid]=[] if not text else [dict(evidence_id='synthetic-'+key,invoice_id=iid,source_type=source,body=text)]
            state=dict(proposed_action={'tool':p.tool,'args':p.args},operator_request=s.mandate.task,invoice_text=[s.invoices[iid].get('text','')],evidence=s.evidence[iid])
            rows.append(dict(id=key,label=key.title()+' receiving',state=state,snapshot=dict(session=s.model_dump(mode='json',exclude={'agent_token_hash','events','outbox'}),pending=[],evaluation_time=now().isoformat()),proposal=p.model_dump()))
        isolated.store.db.close()
        return rows
    @app.get('/api/policy-scenarios',dependencies=[Depends(reader)])
    @route_error
    def scenarios(): return suite()
    def hard(snapshot,proposal,bundle):
        raw=copy.deepcopy(snapshot['session']);raw['agent_token_hash']='experiment';raw['events']=snapshot.get('pending',[])
        s=ControlSession.model_validate(raw);s.mandate.budget_cents=bundle['financial']['budget_cents'];s.mandate.auto_limit_cents=bundle['financial']['auto_limit_cents']
        return [c.model_dump() for c in gateway.constraints(s,Proposal.model_validate(proposal),evaluation_time=datetime.fromisoformat(snapshot['evaluation_time']))]
    def verdict(checks): return 'deny' if any(c['outcome']=='fail' for c in checks) else 'escalate' if any(c['outcome'] in ('review','unavailable') for c in checks) else 'allow'
    @app.post('/api/policy-comparisons',dependencies=[Depends(operator)])
    @route_error
    def compare(body:dict):
        d=get(body['draft_id'],'draft')
        if d['revision']!=body['revision']: raise HTTPException(409,'Save and reload the current draft')
        b=d['bundle'];base=version(body.get('baseline_id',d['parent']));mode=body.get('mode','fresh')
        if mode not in ('fresh','reuse'): raise ValueError('Unknown comparison mode')
        rows=suite();required=True
        if body.get('scenarios') is not None:
            rows=body['scenarios'];required=False
        elif body.get('session_id'):
            s=gateway.get(body['session_id']);required=False;rows=[]
            for e in s.events:
                if e.semantic_evaluation: rows.append(dict(id=e.event_id,label=e.args.get('memo'),state=e.semantic_evaluation['state'],snapshot=e.financial_snapshot,proposal=dict(request_id=e.request_id,tool=e.tool,args=e.args),stored=e.semantic_evaluation,original=e.verdict,hard=[c.model_dump() for c in e.checks if c.kind!='semantic']))
        if not isinstance(rows,list) or not 1<=len(rows)<=8 or len(json.dumps(rows))>250000: raise ValueError('Compare 1–8 bounded scenarios at a time')
        if mode=='reuse' and request(base,{})!=request(b,{}): raise ValueError('Changed Jev inputs require fresh evaluation')
        out=[];calls=0
        for row in rows:
            result={'id':row['id'],'label':row.get('label',row['id']),'original':row.get('original')}
            try:
                state=row['state'];saved=row.get('stored') if body.get('session_id') else None
                compatible=saved and request(migrate(saved['policy']),state)==request(base,state)
                if mode=='reuse' and not compatible: raise ValueError('No compatible stored baseline answers; run fresh evaluation')
                if compatible: baseline=copy.deepcopy(saved);baseline_checks,baseline['queue']=compose(baseline['answers'],base);baseline['reused']=True
                else:
                    baseline_checks,baseline=evaluate(gateway.reviewer.client,state,base,gateway.reviewer.backend=='offline');calls+=int(gateway.reviewer.backend!='offline')
                if mode=='reuse' or request(base,state)==request(b,state):
                    draft=copy.deepcopy(baseline);draft_checks,draft['queue']=compose(draft['answers'],b);draft['reused']=True
                else:
                    draft_checks,draft=evaluate(gateway.reviewer.client,state,b,gateway.reviewer.backend=='offline');calls+=int(gateway.reviewer.backend!='offline')
                for name,policy,semantic,evaluation in [('baseline',base,baseline_checks,baseline),('draft',b,draft_checks,draft)]:
                    financial=hard(row['snapshot'],row['proposal'],policy) if row.get('snapshot') else row.get('hard',[])
                    checks=financial+semantic
                    result[name]=dict(verdict=verdict(checks),queue=evaluation['queue'],checks=checks,evaluation=evaluation,request=request(policy,state),financial_scope='captured state' if row.get('snapshot') else 'semantic only; historical checks frozen')
                if required:
                    amount=224000;f=b['financial'];expected='deny' if amount>f['budget_cents'] or row['id']=='dispute' else 'escalate' if amount>f['auto_limit_cents'] or (b['require_confirmation'] and row['id'] in ('missing','supplier')) else 'allow'
                    result.update(expected=expected,passed=result['draft']['verdict']==expected)
            except Exception as exc: result['error']=str(exc)
            out.append(result)
        result=dict(id='CMP-'+uuid.uuid4().hex[:12],draft_id=d['id'],revision=d['revision'],bundle_hash=b['hash'],baseline_id=base['id'],compiler_version=COMPILER,scenario_hash=hashed(rows),required_suite=required,passed=required and all(r.get('passed',False) for r in out),complete=all('error' not in r for r in out),rows=out,model_calls=calls,side_effect_free=True,independent_scenarios=True,backend=gateway.reviewer.backend,actor='local_operator',created_at=now().isoformat())
        with gateway.store.db: put(result['id'],'comparison',result)
        return result
    @app.get('/api/policy-comparisons/{key}',dependencies=[Depends(reader)])
    @route_error
    def comparison(key:str): return get(key,'comparison')
    @app.post('/api/policy-versions',dependencies=[Depends(operator)])
    @route_error
    def save(body:dict):
        d=get(body['draft_id'],'draft')
        if d['revision']!=body['revision']: raise HTTPException(409,'Draft revision changed')
        b=validate(d['bundle'])
        with gateway.store.db:
            put(b['id'],'bundle',b)
            key='META-'+b['id']
            if not gateway.store.db.execute('SELECT 1 FROM showcase_objects WHERE id=?',(key,)).fetchone(): put(key,'version_metadata',dict(version_id=b['id'],actor='local_operator',created_at=now().isoformat(),parent=d['parent'],draft_id=d['id']))
        return b
    @app.post('/api/policy-activations',dependencies=[Depends(operator)])
    @route_error
    def activate(body:dict):
        b=version(body['version_id']);c=get(body['comparison_id'],'comparison')
        if active()!=body['expected_active']: raise HTTPException(409,'Active version changed; reload')
        if c['bundle_hash']!=b['hash'] or not c['passed'] or not c['complete'] or c['compiler_version']!=COMPILER or c['backend']!=gateway.reviewer.backend: raise ValueError('Activation requires a passing required suite for this exact version')
        a=dict(id='ACT-'+uuid.uuid4().hex[:12],version_id=b['id'],previous_version=active(),comparison_id=c['id'],actor='local_operator',created_at=now().isoformat())
        with gateway.store.db:
            put(a['id'],'activation',a);put('studio-active','active',{'version_id':b['id']})
        return a
