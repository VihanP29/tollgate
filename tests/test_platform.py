"""Current canonical gateway acceptance: isolation, durable money, jobs and recovery."""
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from tollgate.auth import Principal
from tollgate.authorization import AuthorizationGateway, AuditStore, Proposal
from tollgate.decision_context import ProposalV1, Payment, DecisionInputV1, ExecutorOutcome, identity, utcnow
from tollgate.evaluation.datasets import manifest, build_case, validate_manifest
from tollgate.evaluation.evaluator import semantic_state, compiled_request, request_identity, evaluate_input
from tollgate.evaluation.lab import (create_experiment, experiment_result, save_policy, subscribe_shadow,
                                     activate_policy, EvaluationRepository)
from tollgate.jobs.worker import Worker
from tollgate.platform_server import create_app
from tollgate.services.platform import Platform, Conflict, QuotaExceeded
from tollgate.services.runs import start_run
from tollgate.settings import Settings
from tollgate.storage import schema as s
from tollgate.storage.database import Database, row, change
from tollgate.storage.legacy_import import import_legacy
from tollgate.storage.migrate import upgrade


@pytest.fixture
def platform(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'platform.sqlite')
    upgrade(url)
    db = Database(url)
    settings = Settings(database_url=url)
    p = Platform(db, settings, 'op')
    yield p
    db.dispose()


def setup(p, actor=None, invoices=None, **limits):
    actor = actor or Principal('local-owner')
    wid = p.provision(actor, 'workspace')['id']
    mandate = p.create_mandate(actor, wid, invoices or ['INV-1042', 'INV-7002'], limits.pop('budget', 6000000), limits.pop('auto', 1000000), **limits)
    credential = p.issue_credential(actor, wid, mandate['id'])
    agent = p.identity.authenticate(credential['agent_token'])
    return actor, wid, mandate['id'], agent, credential


def payment(request='test', iid='INV-1042', amount=420000, vendor='V-ACME'):
    return ProposalV1(request_id=request, action=Payment(invoice_id=iid, amount_minor=amount,
                       beneficiary_ref=vendor, scheduled_date=utcnow().date()))


def drain(p, limit=50, executor=None):
    worker = Worker(p, executor)
    for _ in range(limit):
        if not worker.tick():
            return
    raise AssertionError('Job processing failed to settle')


def count(p, table, wid):
    with p.db.transaction() as c:
        return c.execute(select(func.count()).select_from(table).where(table.c.workspace_id == wid)).scalar_one()


def test_migrations_repeat_and_schema_ready(platform):
    assert platform.db.ready()
    upgrade(platform.settings.database_url)
    assert platform.db.ready()
    with platform.db.transaction() as c:
        assert c.execute(select(s.usage).where(s.usage.c.scope == 'global')).one().calls == 0


def test_complete_execution_retry_receipt_and_restart(platform):
    owner, wid, mid, agent, credential = setup(platform)
    result = platform.submit(agent, wid, mid, payment())
    assert result['state'] == 'evaluating' and not result['executed']
    assert platform.submit(agent, wid, mid, payment())['id'] == result['id']
    resumed = Platform(Database(platform.settings.database_url), platform.settings, 'op')
    drain(resumed)
    result = resumed.proposal(agent, wid, result['id'])
    assert result['state'] == 'confirmed' and result['executed']
    receipt = resumed.receipt(agent, wid, result['receipt_id'])
    assert receipt['payload']['kind'] == 'mock_payment'
    assert receipt['payload']['settlement'] == 'not_applicable'
    assert count(platform, s.receipts, wid) == 1
    with platform.db.transaction() as c:
        mandate = row(c, s.mandates, wid, mid)
        assert mandate['spent_minor'] == 420000 and mandate['reserved_minor'] == 0
    with pytest.raises(Conflict):
        platform.submit(agent, wid, mid, payment(amount=420001))
    resumed.db.dispose()


def test_cross_workspace_and_credential_scoping(platform):
    owner, w1, m1, a1, _ = setup(platform)
    other, w2, m2, a2, _ = setup(platform, Principal('other'))
    assert platform.list_resources(owner, w1, s.policies)[0]['id'] == platform.list_resources(other, w2, s.policies)[0]['id']
    with pytest.raises(LookupError): platform.list_resources(owner, w2, s.policies)
    with pytest.raises(LookupError): platform.list_resources(a1, w2, s.invoices, m2)
    with pytest.raises(LookupError): platform.submit(a1, w2, m2, payment())
    with pytest.raises(LookupError): platform.submit(a1, w1, m2, payment())
    proposal = platform.submit(a2, w2, m2, payment())
    with pytest.raises(LookupError): platform.proposal(a1, w2, proposal['id'])
    assert count(platform, s.receipts, w1) == 0


def test_revoked_credential_blocks_poll_but_accepted_work_continues(platform):
    owner, wid, mid, agent, credential = setup(platform)
    proposal = platform.submit(agent, wid, mid, payment())
    platform.revoke_credential(owner, wid, credential['id'])
    with pytest.raises(PermissionError): platform.identity.authenticate(credential['agent_token'])
    with pytest.raises(LookupError): platform.proposal(agent, wid, proposal['id'])
    with pytest.raises(LookupError): platform.submit(agent, wid, mid, payment('second'))
    drain(platform)
    assert platform.proposal(owner, wid, proposal['id'])['state'] == 'confirmed'


def test_new_credential_cannot_poll_previous_identity(platform):
    owner, wid, mid, agent, _ = setup(platform)
    old = platform.submit(agent, wid, mid, payment())
    credential = platform.issue_credential(owner, wid, mid)
    other = platform.identity.authenticate(credential['agent_token'])
    with pytest.raises(LookupError): platform.proposal(other, wid, old['id'])
    with pytest.raises(LookupError): platform.submit(other, wid, mid, payment())


def test_cross_mandate_duplicate_claim(platform):
    owner, wid, mid, agent, _ = setup(platform)
    first = platform.submit(agent, wid, mid, payment())
    drain(platform)
    second_mandate = platform.create_mandate(owner, wid, ['INV-1042'], 6000000, 1000000)['id']
    token = platform.issue_credential(owner, wid, second_mandate)['agent_token']
    second = platform.submit(platform.identity.authenticate(token), wid, second_mandate, payment('new-request'))
    assert second['verdict'] == 'deny' and 'duplicate_payment' in second['reason_codes']
    assert count(platform, s.receipts, wid) == 1


def test_pending_review_reservation_prevents_duplicates_and_rejection_releases(platform):
    owner, wid, mid, agent, _ = setup(platform)
    result = platform.submit(agent, wid, mid, payment(iid='INV-7002', amount=224000, vendor='V-HOOLI'))
    drain(platform)
    assert platform.proposal(agent, wid, result['id'])['state'] == 'awaiting_review'
    assert platform.submit(agent, wid, mid, payment('duplicate', 'INV-7002', 224000, 'V-HOOLI'))['verdict'] == 'deny'
    review = platform.list_resources(owner, wid, s.reviews)[0]
    platform.resolve(owner, wid, review['id'], 'reject', 1)
    with platform.db.transaction() as c:
        assert row(c, s.mandates, wid, mid)['reserved_minor'] == 0
    assert count(platform, s.receipts, wid) == 0


def test_evidence_stales_approval_and_explicit_reassessment(platform):
    owner, wid, mid, agent, _ = setup(platform)
    result = platform.submit(agent, wid, mid, payment(iid='INV-7002', amount=224000, vendor='V-HOOLI'))
    drain(platform)
    review = platform.list_resources(owner, wid, s.reviews)[0]
    old_input = platform.proposal(owner, wid, result['id'])['input']
    platform.append_evidence(owner, wid, 'INV-7002', 'Receiving confirms complete delivery for INV-7002; goods accepted.', 'verified_receiving')
    assert platform.proposal(agent, wid, result['id'])['state'] == 'cancelled'
    with pytest.raises(Conflict): platform.resolve(owner, wid, review['id'], 'approve', 1)
    assert count(platform, s.receipts, wid) == 0
    new = platform.reassess(owner, wid, result['id'])
    drain(platform)
    assert platform.proposal(owner, wid, new['id'])['state'] == 'confirmed'
    with platform.db.transaction() as c:
        assert row(c, s.inputs, wid, old_input['id']) == old_input
    assert count(platform, s.receipts, wid) == 1


def test_supplier_cannot_establish_receiving_and_cross_invoice_supersession_denied(platform):
    owner, wid, mid, agent, _ = setup(platform)
    platform.append_evidence(owner, wid, 'INV-7002', 'Receiving complete delivery confirmed and accepted.', 'supplier')
    result = platform.submit(agent, wid, mid, payment(iid='INV-7002', amount=224000, vendor='V-HOOLI'))
    drain(platform)
    assert platform.proposal(agent, wid, result['id'])['state'] == 'awaiting_review'
    with platform.db.transaction() as c:
        eid = c.execute(select(s.evidence.c.id).where(s.evidence.c.workspace_id == wid, s.evidence.c.invoice_id == 'INV-1042')).scalar_one()
    with pytest.raises(ValueError): platform.append_evidence(owner, wid, 'INV-7002', 'Correction', 'verified_receiving', eid)


def test_assigned_distinct_reviewers_and_membership_rechecked_at_dispatch(platform):
    owner = Principal('local-owner')
    wid = platform.provision(owner, 'workspace')['id']
    with platform.db.transaction(wid) as c:
        for actor in ['reviewer1', 'reviewer2']:
            c.execute(s.memberships.insert().values(workspace_id=wid, actor_id=actor, role='reviewer', active=True))
    owner, wid, mid, agent, _ = setup(platform, auto=0, required_approvals=2, reviewers=['reviewer1', 'reviewer2'])
    proposal = platform.submit(agent, wid, mid, payment())
    drain(platform)
    review = platform.list_resources(owner, wid, s.reviews)[0]
    with pytest.raises(LookupError): platform.resolve(owner, wid, review['id'], 'approve', 1)
    r = platform.resolve(Principal('reviewer1'), wid, review['id'], 'approve', 1)
    assert r['state'] == 'awaiting_review'
    with pytest.raises(Conflict): platform.resolve(Principal('reviewer1'), wid, review['id'], 'approve', 2)
    r = platform.resolve(Principal('reviewer2'), wid, review['id'], 'approve', 2)
    assert r['state'] == 'approved'
    with platform.db.transaction(wid) as c:
        c.execute(s.memberships.update().where(s.memberships.c.actor_id == 'reviewer1', s.memberships.c.workspace_id == wid).values(active=False))
    drain(platform)
    assert platform.proposal(agent, wid, proposal['id'])['state'] == 'cancelled'
    assert count(platform, s.receipts, wid) == 0


def test_revocation_before_dispatch_cancels_and_releases_once(platform):
    owner, wid, mid, agent, _ = setup(platform)
    proposal = platform.submit(agent, wid, mid, payment())
    Worker(platform).tick()  # evaluation creates the durable execution intent only
    assert platform.proposal(agent, wid, proposal['id'])['state'] == 'queued'
    platform.revoke_mandate(owner, wid, mid)
    platform.revoke_mandate(owner, wid, mid)
    drain(platform)
    assert count(platform, s.receipts, wid) == 0
    with platform.db.transaction() as c:
        assert row(c, s.mandates, wid, mid)['reserved_minor'] == 0


def test_ambiguous_dispatch_keeps_claim_and_does_not_blind_retry(platform):
    class Uncertain:
        safe_retry = False
        calls = 0
        lookups = 0
        def dispatch(self, *args):
            self.calls += 1
            raise TimeoutError('request body must never be logged')
        def lookup(self, *args):
            self.lookups += 1
            return ExecutorOutcome(state='unknown', error_code='provider_lookup_unavailable')
    owner, wid, mid, agent, _ = setup(platform)
    proposal = platform.submit(agent, wid, mid, payment())
    executor = Uncertain()
    drain(platform, executor=executor)
    assert platform.proposal(agent, wid, proposal['id'])['state'] == 'unknown'
    platform.revoke_mandate(owner, wid, mid)
    with platform.db.transaction() as c:
        intent = c.execute(select(s.intents).where(s.intents.c.workspace_id == wid)).mappings().one()
        assert row(c, s.mandates, wid, mid)['reserved_minor'] == 420000
    from tollgate.services.execution import ExecutionService
    ExecutionService(platform, executor).execute(wid, intent['id'])
    assert executor.calls == 1 and executor.lookups == 1
    assert count(platform, s.receipts, wid) == 0


def test_lease_restart_and_duplicate_job_delivery_single_receipt(platform):
    owner, wid, mid, agent, _ = setup(platform)
    result = platform.submit(agent, wid, mid, payment())
    first = Worker(platform)
    job = first.claim()
    assert job
    with platform.db.transaction(wid) as c:
        change(c, s.jobs, wid, job['id'], lease_until=time.time()-1)
    drain(platform)
    with platform.db.transaction(wid) as c:
        for j in c.execute(select(s.jobs).where(s.jobs.c.workspace_id == wid)).mappings().all():
            change(c, s.jobs, wid, j['id'], state='queued', available_at=time.time())
    drain(platform)
    assert count(platform, s.receipts, wid) == 1
    assert platform.proposal(agent, wid, result['id'])['state'] == 'confirmed'


def test_context_change_during_evaluation_requeues_without_using_stale_answers(platform, monkeypatch):
    import tollgate.services.platform as mod
    owner, wid, mid, agent, _ = setup(platform)
    original = mod.evaluate_input
    def mutate(value):
        platform.append_evidence(owner, wid, 'INV-1042', 'Supplier note; please expedite.', 'supplier')
        return original(value)
    monkeypatch.setattr(mod, 'evaluate_input', mutate)
    result = platform.submit(agent, wid, mid, payment())
    drain(platform)
    assert platform.proposal(agent, wid, result['id'])['state'] == 'held'
    assert count(platform, s.receipts, wid) == 0
    with platform.db.transaction() as c:
        assert all(r.stale for r in c.execute(select(s.evaluations).where(s.evaluations.c.workspace_id == wid)))


def test_evaluation_does_not_hold_financial_write_lock(platform, monkeypatch):
    import tollgate.services.platform as mod
    owner, wid, mid, agent, _ = setup(platform)
    original = mod.evaluate_input
    def check_other_transaction(value):
        # A separate connection writes while semantic work is in progress.
        with ThreadPoolExecutor(1) as pool:
            pool.submit(platform.append_evidence, owner, wid, 'INV-7002', 'Operator note', 'operator_note').result(timeout=3)
        return original(value)
    monkeypatch.setattr(mod, 'evaluate_input', check_other_transaction)
    result = platform.submit(agent, wid, mid, payment())
    drain(platform)
    assert platform.proposal(agent, wid, result['id'])['state'] == 'confirmed'


def test_durable_run_wait_resume_after_restart(platform):
    owner, wid, mid, agent, _ = setup(platform)
    run = start_run(platform, owner, wid, mid, 'run-stable')
    assert start_run(platform, owner, wid, mid, 'run-stable')['id'] == run['id']
    drain(platform)
    assert platform.list_resources(owner, wid, s.runs)[0]['state'] == 'waiting_for_review'
    restarted = Platform(Database(platform.settings.database_url), platform.settings, 'op')
    review = restarted.list_resources(owner, wid, s.reviews)[0]
    restarted.resolve(owner, wid, review['id'], 'approve', review['revision'])
    drain(restarted)
    result = restarted.list_resources(owner, wid, s.runs)[0]
    assert result['state'] == 'finished' and result['payload']['all_paid']
    assert count(platform, s.receipts, wid) == 2
    restarted.db.dispose()


def test_lab_no_mutations_and_general_judge_unavailable_not_accuracy(platform):
    owner, wid, mid, agent, _ = setup(platform)
    with platform.db.transaction() as c:
        before = row(c, s.mandates, wid, mid)
    policy = platform.list_resources(owner, wid, s.policies)[0]['id']
    e = create_experiment(platform, owner, wid, 'synthetic-v1', 'development', [policy])
    drain(platform)
    result = experiment_result(platform, owner, wid, e['id'])
    assert result['state'] == 'completed' and len(result['rows']) == 240
    assert not result['manifest']['admission_eligible']
    general = next(v for k,v in result['metrics'].items() if k.startswith('general_llm_unavailable:'))
    assert general['semantic_available_match']['denominator'] == 0 and general['availability']['unavailable'] > 0
    with platform.db.transaction() as c:
        assert row(c, s.mandates, wid, mid) == before
    for table in [s.receipts, s.reservations, s.reviews, s.intents, s.claims]:
        assert count(platform, table, wid) == 0
    repository = EvaluationRepository(platform.db)
    with pytest.raises(ValueError): repository.put_result(s.mandates, wid, {})
    with pytest.raises(ValueError): repository.get(s.credentials, wid, 'anything')


def test_shadow_disagreement_and_job_quota_does_not_change_execution(platform):
    owner, wid, mid, agent, _ = setup(platform)
    version = platform.list_resources(owner, wid, s.policies)[0]['payload']
    candidate = copy.deepcopy(version); candidate['require_confirmation'] = False; candidate['name'] = 'Dispute controls'
    candidate = save_policy(platform, owner, wid, candidate)
    subscribe_shadow(platform, owner, wid, candidate['id'])
    result = platform.submit(agent, wid, mid, payment(iid='INV-7002', amount=224000, vendor='V-HOOLI'))
    drain(platform)
    assert platform.proposal(agent, wid, result['id'])['state'] == 'awaiting_review'
    shadow = platform.list_resources(owner, wid, s.shadow_rows)[0]['payload']
    assert shadow['active_verdict'] == 'review' and shadow['candidate_verdict'] == 'allow'
    assert shadow['new_provider_calls'] == 0 and count(platform, s.receipts, wid) == 0
    assert count(platform, s.reservations, wid) == 1


def test_dataset_split_and_label_isolation_and_immutable_context():
    data = manifest(); assert validate_manifest(data)
    assert data['case_count'] == 120 and data['family_count'] == 24
    for split in ['development', 'held_out']:
        for label in ['allow', 'must_review', 'must_deny']:
            assert sum(c['split'] == split and c['expected'] == label for c in data['cases']) >= 10
    case = data['cases'][0]; value = build_case(case)
    sent = json.dumps(compiled_request(value))
    assert all(key not in sent for key in ['expected', 'rationale', 'adjudication', 'family_id', 'held_out'])
    with pytest.raises(Exception): value.evidence_json = '[]'
    changed = value.model_copy(update={'evidence_revision': 2, 'evidence_json': '[]'})
    assert changed.input_hash != value.input_hash and request_identity(changed) != request_identity(value)
    candidate = copy.deepcopy(value.unpack('policy')); candidate['review_at'] = .2
    assert request_identity(value, candidate) == request_identity(value)
    candidate['questions'][0]['instructions'] += ' Different question.'
    assert request_identity(value, candidate) != request_identity(value)


def test_explicit_legacy_import_dry_run_idempotence_and_failure(platform, tmp_path):
    owner, wid, mid, agent, _ = setup(platform)
    path = tmp_path/'legacy.sqlite'
    g = AuthorizationGateway(store=AuditStore(str(path)))
    session, token = g.create()
    event = g.submit(session.session_id, Proposal(tool='schedule_payment', args=dict(vendor_id='V-ACME',amount_usd=4200,date=utcnow().date().isoformat(),memo='INV-1042')), token)
    assert event.execution == 'executed'
    before = path.read_bytes()
    report = import_legacy(platform.db, path, wid)
    assert report['new_archives'] == 1 and count(platform, s.legacy, wid) == 0
    assert import_legacy(platform.db, path, wid, True)['receipts'] == 1
    assert import_legacy(platform.db, path, wid, True)['already_imported'] == 1
    assert path.read_bytes() == before
    assert count(platform, s.receipts, wid) == 0
    with pytest.raises(LookupError): import_legacy(platform.db, path, 'missing-workspace', True)
    assert path.read_bytes() == before


def test_scoped_reads_no_money_or_semantic_work(platform):
    owner, wid, mid, agent, _ = setup(platform)
    invoices = platform.list_resources(agent, wid, s.invoices, mid)
    assert len(invoices) == 2 and 'bank_account' not in json.dumps(invoices)
    vendors = platform.list_resources(agent, wid, s.vendors, mid)
    assert 'bank_account' not in json.dumps(vendors)
    result = platform.submit(agent, wid, mid, ProposalV1.model_validate(dict(request_id='read',action=dict(tool='read_invoice',invoice_id='INV-1042'))))
    assert not result['executed'] and result['result']['invoice_id'] == 'INV-1042'
    for table in [s.reservations, s.claims, s.intents, s.evaluations, s.jobs]:
        assert count(platform, table, wid) == 0


def test_settings_fail_closed_and_minimums():
    with pytest.raises(ValueError): Settings(mode='hosted')
    with pytest.raises(ValueError): Settings(live_jev_enabled=True)
    with pytest.raises(ValueError): Settings(claims_mode='enforce')
    with pytest.raises(ValueError): Settings(vision_enabled=True)
    assert Settings.from_env().mode == 'local'


def test_api_role_denial_origin_and_bounds(platform):
    app = create_app(platform.settings, platform.db, 'op')
    c = TestClient(app); h = {'Authorization':'Bearer op'}
    assert c.get('/api/v1/me').status_code == 401
    w = c.post('/api/v1/demo-workspaces',json={'request_id':'api'},headers=h).json()['id']
    url = f'/api/v1/workspaces/{w}'
    assert c.post(url+'/mandates',headers=h,json={'invoice_ids':['INV-1042'],'actor':'fake'}).status_code == 422
    assert c.post(url+'/mandates',headers=h,json={'invoice_ids':['INV-1042'],'budget_minor':True}).status_code == 422
    assert c.post('/api/v1/demo-workspaces',headers={**h,'Origin':'https://attacker.example'},json={'request_id':'x'}).status_code == 403
    assert c.post('/api/v1/demo-workspaces',headers=h,content=b'x'*70000).status_code == 413
    assert c.get('/health/live').status_code == 200
    assert c.get('/health/ready').status_code == 503
    Worker(app.state.platform).heartbeat()
    assert c.get('/health/ready').status_code == 200
    m = c.post(url+'/mandates',headers=h,json={'invoice_ids':['INV-1042']}).json()['id']
    credential = c.post(url+'/agent-credentials',headers=h,json={'mandate_id':m}).json()
    ah = {'Authorization':'Bearer '+credential['agent_token']}
    assert c.get(url+'/invoices',headers=ah).status_code == 404
    assert c.get(url+f'/mandates/{m}/invoices',headers=ah).status_code == 200
    assert c.post(url+'/policies',headers=ah,json={'bundle':{}}).status_code == 404
    assert c.post(url+'/memberships',headers=ah,json={'actor_id':'evil','role':'admin'}).status_code == 404
    assert c.get(url+'/agent-credentials',headers=ah).status_code == 404
    assert c.get(url+'/connections',headers=ah).status_code == 404


def test_full_fixture_batch_does_not_consume_stale_retries_in_queue(platform):
    owner, wid, mid, agent, _ = setup(platform, invoices=['INV-1042','INV-2210','INV-5120','INV-7002','INV-6408','INV-8090'])
    start_run(platform, owner, wid, mid, 'all-six')
    drain(platform)
    proposals = platform.list_resources(owner, wid, s.proposals)
    states = {p['payload']['action']['invoice_id']:p['state'] for p in proposals}
    assert states == {'INV-1042':'confirmed','INV-2210':'confirmed','INV-5120':'confirmed',
                      'INV-7002':'awaiting_review','INV-6408':'denied','INV-8090':'awaiting_review'}
    assert all(p['rechecks'] == 0 for p in proposals)


def test_reset_generation_preserves_history_and_duplicate_identity(platform):
    owner, wid, mid, agent, credential = setup(platform)
    result = platform.submit(agent, wid, mid, payment())
    drain(platform)
    snapshot = platform.proposal(owner, wid, result['id'])['input']
    w = platform.workspaces(owner)[0]
    changed = platform.reset_synthetic(owner, wid, w['revision'])
    assert changed['generation'] != w['generation'] and not changed['usage_reset']
    with pytest.raises(PermissionError): platform.identity.authenticate(credential['agent_token'])
    new_mid = platform.create_mandate(owner, wid, ['INV-1042'], 6000000, 1000000)['id']
    token = platform.issue_credential(owner, wid, new_mid)['agent_token']
    new = platform.submit(platform.identity.authenticate(token), wid, new_mid, payment())
    drain(platform)
    assert platform.proposal(owner, wid, new['id'])['state'] == 'confirmed'
    assert count(platform,s.receipts,wid) == 2
    with platform.db.transaction() as c:
        assert row(c,s.inputs,wid,snapshot['id']) == snapshot


def test_single_process_local_launch_uses_durable_jobs(platform):
    owner,wid,mid,agent,credential=setup(platform)
    with TestClient(create_app(platform.settings,platform.db,'op',embedded_worker=True)) as c:
        result=platform.submit(agent,wid,mid,payment())
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            current=platform.proposal(agent,wid,result['id'])
            if current['state']=='confirmed':break
            time.sleep(.02)
        assert current['state']=='confirmed'
        assert c.get('/health/ready').status_code==200


def test_claim_payload_changes_conflict_even_when_claims_disabled(platform):
    owner,wid,mid,agent,_=setup(platform)
    original=payment();platform.submit(agent,wid,mid,original)
    changed=ProposalV1.model_validate({**original.model_dump(mode='json'),'claims':[dict(claim_id='c1',role='delivery_confirmation',
        statement='All goods were accepted.',invoice_id='INV-1042',source_ids=[])]})
    with pytest.raises(Conflict):platform.submit(agent,wid,mid,changed)


def test_hosted_cannot_start_shared_local_prototype(monkeypatch):
    from tollgate.demo_server import create_control_app
    monkeypatch.setenv('TOLLGATE_MODE','hosted')
    with pytest.raises(ValueError):create_control_app(operator_token='shared')


def test_hosted_demo_has_no_administrative_capability(platform):
    hosted=Settings(mode='hosted',database_url='postgresql+psycopg://unused/unused',public_origin='https://app.example',
        identity_issuer='https://identity.example',identity_audience='tollgate',identity_jwks_url='https://identity.example/jwks')
    sandbox=Platform(platform.db,hosted)
    actor=Principal('jwt:demo-user');wid=sandbox.provision(actor,'demo')['id']
    assert sandbox.workspaces(actor)[0]['role']=='demo'
    with sandbox.db.transaction() as c:
        with pytest.raises(LookupError):sandbox.authorize(c,actor,wid,'admin')
        assert sandbox.authorize(c,actor,wid,'operate').role=='demo'


def test_full_admission_pool_does_not_prevent_accepted_outbox_handoff(platform):
    owner,wid,mid,agent,_=setup(platform)
    bounded=Platform(platform.db,Settings(database_url=platform.settings.database_url,max_jobs=1),'op')
    result=bounded.submit(agent,wid,mid,payment())
    with pytest.raises(QuotaExceeded):bounded.submit(agent,wid,mid,payment('another'))
    drain(bounded)
    assert bounded.proposal(agent,wid,result['id'])['state']=='confirmed'


def test_shadow_admission_exhaustion_cannot_roll_back_payment(platform):
    owner,wid,mid,agent,_=setup(platform)
    bounded=Platform(platform.db,Settings(database_url=platform.settings.database_url,max_jobs=1),'op')
    policy=bounded.list_resources(owner,wid,s.policies)[0]['id']
    subscribe_shadow(bounded,owner,wid,policy)
    with bounded.db.transaction(wid) as c:
        bounded.enqueue(c,wid,'experiment','non-running-fixture','filled-evaluation-pool')
    result=bounded.submit(agent,wid,mid,payment())
    worker=Worker(bounded)
    worker.tick();worker.tick()  # operational decisions and execution precede evaluation work
    assert bounded.proposal(agent,wid,result['id'])['state']=='confirmed'
    assert count(bounded,s.receipts,wid)==1


def test_sqlite_backup_restores_receipts_and_canonical_history(platform,tmp_path):
    import sqlite3
    owner,wid,mid,agent,_=setup(platform)
    result=platform.submit(agent,wid,mid,payment());drain(platform)
    before=platform.proposal(owner,wid,result['id'])
    backup=tmp_path/'restored.sqlite'
    with sqlite3.connect(platform.db.engine.url.database) as source,sqlite3.connect(backup) as target:
        source.backup(target)
    restored=Platform(Database('sqlite:///'+str(backup)),Settings(database_url='sqlite:///'+str(backup)),'op')
    assert restored.db.ready()
    after=restored.proposal(owner,wid,result['id'])
    assert after['receipt']==before['receipt'] and after['input_history']==before['input_history']
    restored.db.dispose()


def test_failed_legacy_import_rolls_back_prior_archives(platform,tmp_path):
    owner,wid,mid,agent,_=setup(platform)
    path=tmp_path/'legacy-failure.sqlite'
    g=AuthorizationGateway(store=AuditStore(str(path)));g.create();second,_=g.create()
    with g.store.db:
        g.store.db.execute('INSERT INTO control_history (session_id,at,kind,payload) VALUES (?,?,?,?)',
                           (second.session_id,'2026-11-12','invalid_fixture','invalid-json'))
    before=path.read_bytes()
    with pytest.raises(ValueError):import_legacy(platform.db,path,wid,True)
    assert count(platform,s.legacy,wid)==0 and path.read_bytes()==before
    with g.store.db:g.store.db.execute("DELETE FROM control_history WHERE kind='invalid_fixture'")
    assert import_legacy(platform.db,path,wid,True)['new_archives']==2


def test_expired_authority_releases_review_and_stops_waiting_run(platform):
    owner,wid,mid,agent,_=setup(platform,auto=0)
    start_run(platform,owner,wid,mid,'expires-during-review')
    drain(platform)
    assert platform.list_resources(owner,wid,s.runs)[0]['state']=='waiting_for_review'
    with platform.db.transaction(wid) as c:
        change(c,s.mandates,wid,mid,expires_at=time.time()-1)
    drain(platform)
    assert platform.list_resources(owner,wid,s.runs)[0]['state']=='cancelled'
    assert all(r['state']=='expired' for r in platform.list_resources(owner,wid,s.reviews))
    with platform.db.transaction() as c:
        assert row(c,s.mandates,wid,mid)['reserved_minor']==0
        assert all(r['payload']['run_id'] for r in c.execute(select(s.inputs).where(s.inputs.c.workspace_id==wid)).mappings())
    assert count(platform,s.receipts,wid)==0


def test_crash_attempt_exhaustion_holds_evaluating_proposal(platform):
    owner,wid,mid,agent,_=setup(platform)
    result=platform.submit(agent,wid,mid,payment())
    worker=Worker(platform)
    claimed=worker.claim()
    with platform.db.transaction(wid) as c:
        change(c,s.jobs,wid,claimed['id'],attempts=platform.settings.max_retries+1,lease_until=time.time()-1)
    assert worker.tick()
    assert platform.proposal(agent,wid,result['id'])['state']=='held'
    with platform.db.transaction() as c:
        assert row(c,s.jobs,wid,claimed['id'])['last_error']=='attempt_limit_recovered'
    assert count(platform,s.receipts,wid)==0


def test_crash_attempt_exhaustion_keeps_ambiguous_execution_reserved(platform):
    owner,wid,mid,agent,_=setup(platform)
    result=platform.submit(agent,wid,mid,payment())
    worker=Worker(platform);worker.tick()
    claimed=worker.claim()
    with platform.db.transaction(wid) as c:
        intent=row(c,s.intents,wid,claimed['object_id'])
        change(c,s.intents,wid,intent['id'],state='dispatching',dispatched_at=time.time())
        change(c,s.proposals,wid,result['id'],state='dispatching')
        change(c,s.jobs,wid,claimed['id'],attempts=platform.settings.max_retries+1,lease_until=time.time()-1)
    worker.tick()
    assert platform.proposal(agent,wid,result['id'])['state']=='unknown'
    with platform.db.transaction() as c:
        assert row(c,s.mandates,wid,mid)['reserved_minor']==420000
    platform.revoke_mandate(owner,wid,mid)
    assert platform.proposal(owner,wid,result['id'])['state']=='unknown'
    assert count(platform,s.receipts,wid)==0
