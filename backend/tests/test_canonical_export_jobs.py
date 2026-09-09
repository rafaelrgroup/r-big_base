"""Synthetic PostgreSQL job protocol; no business records or external services."""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from bigbase.canonical_store import CanonicalStore, json_text
from bigbase.canonical_export_jobs import (CanonicalExportJobs,CanonicalExportWorker,ExportCheckpoint,
    ExportConflict,ExportJobError,ExportLeaseLost,ExportRetryableError,ExportSourceExpired)


@pytest.fixture(scope='module')
def jobs():
    dsn=os.environ.get('BIGBASE_EXPORT_TEST_PG_DSN')
    if not dsn:pytest.skip('Requires explicit isolated export validation DSN')
    with psycopg.connect(dsn) as c:
        name,port,address,version=c.execute("SELECT current_database(),current_setting('port'),inet_server_addr()::text,current_setting('server_version_num')::int").fetchone()
        assert (name,port) in {('bigbase_test','18769'),('bigbase_release_validation','15432')}
        assert address in {None,'127.0.0.1'} and 180000<=version<190000
    repo=CanonicalStore(dsn,'exportjob_'+uuid4().hex)
    info=repo.initialize(environment='synthetic')
    store=CanonicalExportJobs(repo,expected_deployment_id=info['deployment_id']);store.initialize_synthetic()
    try:yield store
    finally:
        with repo.connection() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(repo.schema)))


@pytest.fixture
def owner():return str(uuid4())


def create(jobs,owner,body=None,**extra):
    return jobs.create(body or {'entity_type':'person','filters':{'all':[]}},principal_id=owner,
        authorization_sha256='a'*64,idempotency_key='test-key-'+uuid4().hex,owner_limit=100,global_limit=1000,**extra)


def expire_lease(jobs,id):
    with jobs.connection() as c:c.execute("UPDATE canonical_export_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=%s",(id,))


def claim_specific(jobs,job):
    # Other tests deliberately leave jobs for audit. Do not delete their history;
    # cancel only the synthetic work belonging to those test cases.
    with jobs.connection() as c:
        for row in c.execute("SELECT job_id,principal_id FROM canonical_export_jobs WHERE status IN ('pending','preparing') AND job_id<>%s",(job['job_id'],)).fetchall():
            c.execute("UPDATE canonical_export_jobs SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE job_id=%s",(row['job_id'],))
    return jobs.claim('synthetic-worker')


def step(job,phase=None,**values):
    return ExportCheckpoint(str(uuid4()),job['checkpoint'],phase or job['phase'],values.pop('processed',0),**values)


def test_request_exact_types_isolation_and_idempotency(jobs,owner):
    body={'decimal':Decimal('12345678901234567890.1200'),'large':2**100,'values':[None,False,0,'','a\0b\ud800'],
        'formula':'=HYPERLINK("synthetic.invalid")'}
    key='idempotency-'+uuid4().hex
    job=jobs.create(body,principal_id=owner,authorization_sha256='a'*64,idempotency_key=key,owner_limit=100,global_limit=1000)
    assert json_text(jobs.get(job['job_id'],principal_id=owner)['request'])==json_text(body)
    assert jobs.create(body,principal_id=owner,authorization_sha256='a'*64,idempotency_key=key)['job_id']==job['job_id']
    with pytest.raises(ExportConflict):jobs.create({'changed':True},principal_id=owner,authorization_sha256='a'*64,idempotency_key=key)
    with pytest.raises(ExportJobError,match='NOT_FOUND'):jobs.get(job['job_id'],principal_id=str(uuid4()))
    public=jobs.public(job)
    assert not {'request','cursor','cut','authorization_sha256','lease_token','api_key_ref','principal_id'} & public.keys()
    assert 'HYPERLINK' not in str(public)


def test_checkpoints_survive_restart_and_old_worker_is_fenced(jobs,owner):
    job=claim_specific(jobs,create(jobs,owner))
    first=step(job,processed=2,total=10,total_relation='eq',cursor={'opaque':'synthetic-cursor','position':[False,0,None]})
    done=jobs.checkpoint(job['job_id'],job['lease_token'],first)
    assert done['checkpoint']==1 and done['processed']==2
    assert jobs.checkpoint(job['job_id'],job['lease_token'],first)['checkpoint']==1
    expire_lease(jobs,job['job_id'])
    restarted=CanonicalExportJobs(jobs.repository,expected_deployment_id=jobs.deployment_id).claim('second-worker')
    assert restarted['cursor']==first.cursor and restarted['checkpoint']==1
    assert restarted['lease_token']!=job['lease_token']
    with pytest.raises(ExportLeaseLost):jobs.checkpoint(job['job_id'],job['lease_token'],step(done,processed=3,total=10,total_relation='eq'))
    assert jobs.get(job['job_id'],principal_id=owner)['checkpoint']==1


def test_cancel_revokes_lease_and_does_not_resume(jobs,owner):
    job=claim_specific(jobs,create(jobs,owner));cancelled=jobs.cancel(job['job_id'],principal_id=owner)
    assert cancelled['status']=='cancelled'
    with pytest.raises(ExportLeaseLost):jobs.checkpoint(job['job_id'],job['lease_token'],step(job))
    assert jobs.claim('different-worker') is None
    assert jobs.cancel(job['job_id'],principal_id=owner)['status']=='cancelled'


def test_cut_is_frozen_and_backwards_progress_rolls_back(jobs,owner):
    job=claim_specific(jobs,create(jobs,owner))
    job=jobs.checkpoint(job['job_id'],job['lease_token'],step(job,processed=5,total=9,total_relation='eq',cut={'selection':'synthetic-fixed-cut'}))
    for change in [dict(processed=4,total=9,total_relation='eq'),dict(processed=6,total=10,total_relation='eq'),
                   dict(processed=6,total=9,total_relation='eq',cut={'selection':'changed'})]:
        with pytest.raises(ExportJobError):jobs.checkpoint(job['job_id'],job['lease_token'],step(job,**change))
    assert jobs.get(job['job_id'],principal_id=owner)['checkpoint']==1


def test_zero_results_still_requires_verified_artifact(jobs,owner):
    job=claim_specific(jobs,create(jobs,owner))
    for phase in ('selecting','materializing','writing','verifying','publishing'):
        job=jobs.checkpoint(job['job_id'],job['lease_token'],step(job,phase,total=0,total_relation='eq',cut={'selection':'empty-cut'}))
    with pytest.raises(ExportJobError,match='VERIFIED_EXPORT_ARTIFACT_REQUIRED'):
        jobs.checkpoint(job['job_id'],job['lease_token'],step(job,total=0,total_relation='eq',complete=True))
    part={'part_id':str(uuid4()),'ordinal':0,'file_name':'part-00000000.xlsx','bytes':50,'sha256':'b'*64,'verification_sha256':'c'*64,'rows':{'Resumo':1}}
    result=jobs.checkpoint(job['job_id'],job['lease_token'],step(job,total=0,total_relation='eq',parts=(part,),complete=True))
    assert result['status']=='completed' and jobs.public(result)['progress_percent']==100
    assert (result['expires_at']-result['completed_at']).total_seconds()>=7*86400-1
    with pytest.raises(ExportConflict):jobs.cancel(job['job_id'],principal_id=owner)


def test_history_request_and_part_evidence_cannot_be_overwritten(jobs,owner):
    job=create(jobs,owner)
    for query,params in [
        ("UPDATE canonical_export_jobs SET request_json='{}' WHERE job_id=%s",(job['job_id'],)),
        ('DELETE FROM canonical_export_jobs WHERE job_id=%s',(job['job_id'],)),
        ('UPDATE canonical_export_events SET action=%s WHERE job_id=%s',('changed',job['job_id']))]:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with jobs.connection() as c:c.execute(query,params)


def test_extension_requires_explicit_matching_deployment(jobs):
    wrong=CanonicalExportJobs(jobs.repository,expected_deployment_id=str(uuid4()))
    with pytest.raises(ExportJobError,match='DESTINATION_CHANGED'):wrong.claim('synthetic-worker')
    jobs.initialize_synthetic()


def test_atomic_queue_owner_quota(jobs,owner):
    key='quota-'+uuid4().hex
    jobs.create({},principal_id=owner,authorization_sha256='a'*64,idempotency_key=key,owner_limit=1,global_limit=1000)
    with pytest.raises(ExportJobError,match='QUEUE_QUOTA'):
        jobs.create({},principal_id=owner,authorization_sha256='a'*64,idempotency_key=key+'-second',owner_limit=1,global_limit=1000)


class FakeJobs:
    def __init__(self):
        self.job={'job_id':str(uuid4()),'principal_id':str(uuid4()),'api_key_ref':'synthetic-key',
            'authorization_sha256':'a'*64,'lease_token':str(uuid4()),'checkpoint':0,'phase':'validating'}
        self.committed=[];self.failed=[];self.cancelled=False
    def claim(self,*args,**kwargs):return deepcopy(self.job)
    def checkpoint(self,id,token,step,**kwargs):
        if self.cancelled:raise ExportLeaseLost('EXPORT_LEASE_LOST')
        assert kwargs['release_lease'] is True;self.committed.append(step);return {'status':'preparing'}
    def fail(self,id,token,code):
        if self.cancelled:raise ExportLeaseLost('EXPORT_LEASE_LOST')
        self.failed.append(code);return {'status':'failed','error_code':code}


def worker_fixture():
    jobs=FakeJobs();grant={'active':True,'can_export':True,'api_key_active':True,'authorization_sha256':'a'*64}
    state={'guard_held':False,'checks':0}
    @contextmanager
    def authorize(*_):
        state['checks']+=1;state['guard_held']=True
        try:yield grant
        finally:state['guard_held']=False
    return jobs,grant,state,authorize


def test_worker_live_revocation_between_work_and_checkpoint():
    jobs,grant,state,authorize=worker_fixture()
    def work(job):grant['api_key_active']=False;return step(job)
    worker=CanonicalExportWorker(jobs,authorize=authorize,step=work,verify_part=lambda *_:True)
    assert worker.run_one('worker')['error_code']=='EXPORT_ACCESS_REVOKED'
    assert not jobs.committed and state['checks']==2


def test_worker_holds_authorization_through_commit_and_releases_lease():
    jobs,grant,state,authorize=worker_fixture();original=jobs.checkpoint
    def commit(*a,**kw):assert state['guard_held'];return original(*a,**kw)
    jobs.checkpoint=commit
    worker=CanonicalExportWorker(jobs,authorize=authorize,step=lambda job:step(job),verify_part=lambda *_:True)
    assert worker.run_one('worker')['status']=='preparing' and state['guard_held'] is False


def test_worker_cancelled_or_lost_lease_cannot_finish():
    jobs,grant,state,authorize=worker_fixture()
    def work(job):jobs.cancelled=True;return step(job)
    assert CanonicalExportWorker(jobs,authorize=authorize,step=work,verify_part=lambda *_:True).run_one('worker') is None
    assert not jobs.failed and not jobs.committed


def test_worker_retryable_error_preserves_checkpoint_and_cursor_expiry_is_explicit():
    for failure,expected in [(ExportRetryableError('safe'),None),(ExportSourceExpired('ignored'),'EXPORT_SOURCE_CURSOR_EXPIRED'),(RuntimeError('PERSONAL_INPUT_DO_NOT_LOG'),'EXPORT_STEP_FAILED')]:
        jobs,grant,state,authorize=worker_fixture()
        def work(_):raise failure
        result=CanonicalExportWorker(jobs,authorize=authorize,step=work,verify_part=lambda *_:True).run_one('worker')
        assert (result['error_code'] if result else None)==expected
        assert 'PERSONAL_INPUT_DO_NOT_LOG' not in str(result) and not jobs.committed


def test_strict_checkpoint_types_and_part_paths():
    fake={'checkpoint':0,'phase':'validating'}
    for values in [dict(processed=True),dict(total=1,total_relation=None),dict(cursor={'huge':'x'*70000})]:
        with pytest.raises(ExportJobError):step(fake,**values).body()
    part={'part_id':str(uuid4()),'ordinal':0,'file_name':'../../unsafe.xlsx','bytes':1,'sha256':'a'*64,'verification_sha256':'b'*64,'rows':{}}
    with pytest.raises(ExportJobError):step(fake,parts=(part,)).body()
