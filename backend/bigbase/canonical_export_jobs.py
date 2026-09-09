"""Persistent canonical export jobs. Explicit repository, no daemon/default DSN.

This extension stores requests, checkpoints and verified-part receipts. It does
not select people, render spreadsheets or enable any HTTP endpoint by itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Callable
from uuid import UUID, uuid4

import psycopg

from .canonical_store import json_text, decode, digest

DDL = Path(__file__).resolve().parents[2] / 'infra/sql/010_canonical_export_jobs.sql'
PHASES = ('validating', 'selecting', 'materializing', 'writing', 'verifying', 'publishing')
TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'expired'})
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class ExportJobError(RuntimeError): pass
class ExportLeaseLost(ExportJobError): pass
class ExportConflict(ExportJobError): pass
class ExportAccessRevoked(ExportJobError): pass
class ExportSourceExpired(ExportJobError): pass
class ExportRetryableError(ExportJobError): pass


def require(value, code, exception=ExportJobError):
    if not value: raise exception(code)


def identifier(value):
    try: result = str(UUID(value))
    except (ValueError, TypeError, AttributeError): raise ExportJobError('INVALID_EXPORT_ID') from None
    require(value == result, 'INVALID_EXPORT_ID')
    return result


def sha(value):
    require(type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value), 'INVALID_EXPORT_DIGEST')
    return value


def literal(value, maximum):
    try: result = json_text(value)
    except Exception: raise ExportJobError('INVALID_EXPORT_JSON') from None
    require(len(result.encode('ascii')) <= maximum, 'EXPORT_PAYLOAD_TOO_LARGE')
    return result


def number(value, *, maximum=2**63-1):
    require(type(value) is int and 0 <= value <= maximum, 'INVALID_EXPORT_COUNT')
    return value


@dataclass(frozen=True)
class ExportCheckpoint:
    operation_id: str
    expected_checkpoint: int
    phase: str
    processed: int
    total: int | None = None
    total_relation: str | None = None
    cursor: object = None
    cut: object = None
    parts: tuple = field(default_factory=tuple)
    complete: bool = False

    def body(self):
        identifier(self.operation_id); number(self.expected_checkpoint); number(self.processed)
        require(self.phase in PHASES and type(self.complete) is bool, 'INVALID_EXPORT_PHASE')
        require((self.total is None) == (self.total_relation is None), 'INVALID_EXPORT_TOTAL')
        if self.total is not None:
            number(self.total)
            require(self.total_relation in {'eq', 'gte'} and (self.total_relation != 'eq' or self.processed <= self.total), 'INVALID_EXPORT_TOTAL')
        literal(self.cursor,65536); literal(self.cut,65536)
        require(type(self.parts) in {tuple,list} and len(self.parts)<=100, 'TOO_MANY_EXPORT_PARTS')
        seen=set()
        for part in self.parts:
            require(type(part) is dict and set(part)=={'part_id','ordinal','file_name','bytes','sha256','verification_sha256','rows'}, 'INVALID_EXPORT_PART')
            identifier(part['part_id']); number(part['ordinal'],maximum=2**31-1); number(part['bytes'])
            sha(part['sha256']);sha(part['verification_sha256'])
            require(part['bytes']>0 and part['part_id'] not in seen and type(part['file_name']) is str
                and re.fullmatch(r'part-[0-9]{8}\.(xlsx|zip)',part['file_name']), 'INVALID_EXPORT_PART')
            require(type(part['rows']) is dict and len(part['rows'])<=1000 and all(type(k) is str and type(v) is int and v>=0 for k,v in part['rows'].items()),'INVALID_EXPORT_PART')
            literal(part['rows'],65536);seen.add(part['part_id'])
        result={'phase':self.phase,'processed':self.processed,'total':self.total,'total_relation':self.total_relation,
            'cursor':self.cursor,'cut':self.cut,'parts':list(self.parts),'complete':self.complete}
        literal(result,262144);return result


class CanonicalExportJobs:
    def __init__(self, repository, *, expected_deployment_id: str):
        self.repository=repository;self.deployment_id=identifier(expected_deployment_id)

    def _verify(self,c,*,extension=True):
        info=self.repository._deployment(c)
        require(str(info['deployment_id'])==self.deployment_id,'EXPORT_DESTINATION_CHANGED')
        if extension:
            row=c.execute('SELECT * FROM canonical_export_meta WHERE singleton').fetchone()
            require(row and row['version']==1 and str(row['deployment_id'])==self.deployment_id
                and row['ddl_sha256']==hashlib.sha256(DDL.read_bytes()).hexdigest(),'EXPORT_SCHEMA_MISMATCH')
        return info

    @contextmanager
    def connection(self):
        with self.repository.connection() as c:
            self._verify(c);yield c

    def initialize_synthetic(self):
        """Test-only DDL helper. Real installation is an explicit migration."""
        with self.repository.connection() as c:
            info=self._verify(c,extension=False)
            require(info['environment']=='synthetic','SYNTHETIC_EXPORT_INSTALLATION_REQUIRED')
            c.execute('SELECT pg_advisory_xact_lock(%s)',(5023961458506701,))
            existing=c.execute("SELECT to_regclass('canonical_export_meta') AS name").fetchone()['name']
            if existing:self._verify(c);return
            c.execute(DDL.read_text())
            c.execute('INSERT INTO canonical_export_meta(version,ddl_sha256,deployment_id) VALUES(1,%s,%s)',
                (hashlib.sha256(DDL.read_bytes()).hexdigest(),UUID(self.deployment_id)))

    @staticmethod
    def _row(row):
        if row is None:return None
        result=dict(row)
        for key in ['job_id','lease_token']:
            if result.get(key) is not None:result[key]=str(result[key])
        for key in ['request','cursor','cut']:
            result[key]=decode(result.pop(key+'_json'))
        return result

    @staticmethod
    def public(job):
        known=job['total'] is not None and job['total_relation']=='eq'
        percent=100 if job['status'] in {'completed','expired'} else (min(99,round(100*job['processed']/job['total'],3)) if known and job['total'] else None)
        return {'id':job['job_id'],'status':job['status'],'phase':job['phase'],'processed':job['processed'],
            'total':job['total'],'total_relation':job['total_relation'],'progress_percent':percent,
            'progress_kind':'measured' if known or job['status'] in {'completed','expired'} else 'unknown','progress_scope':'phase',
            'created_at':job['created_at'].isoformat(),'updated_at':job['updated_at'].isoformat(),
            'completed_at':job['completed_at'].isoformat() if job['completed_at'] else None,
            'expires_at':job['expires_at'].isoformat() if job['expires_at'] else None,'error_code':job['error_code'],
            'files_available':job['status']=='completed' and job['expires_at'] is not None and job['expires_at']>datetime.now(timezone.utc)}

    @staticmethod
    def _event(c,job,action,detail=None):
        c.execute('INSERT INTO canonical_export_events(job_id,action,principal_id,checkpoint,detail_json) VALUES(%s,%s,%s,%s,%s)',
            (UUID(job['job_id']),action,job['principal_id'],job['checkpoint'],literal(detail or {},65536)))

    def create(self,request,*,principal_id,api_key_ref=None,authorization_sha256,idempotency_key,owner_limit=2,global_limit=10):
        identifier(principal_id);sha(authorization_sha256)
        require(api_key_ref is None or type(api_key_ref) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,160}',api_key_ref),'INVALID_EXPORT_KEY_REFERENCE')
        require(type(idempotency_key) is str and 8<=len(idempotency_key)<=200 and idempotency_key.isascii(),'EXPORT_IDEMPOTENCY_REQUIRED')
        require(type(request) is dict,'EXPORT_REQUEST_OBJECT_REQUIRED')
        payload=literal(request,MAX_REQUEST_BYTES);request_sha=digest(request)
        key_sha=hashlib.sha256(idempotency_key.encode('ascii')).hexdigest()
        require(type(owner_limit) is int and type(global_limit) is int and 1<=owner_limit<=global_limit<=10000,'INVALID_EXPORT_QUOTA')
        with self.connection() as c:
            # Creation is uncommon; one short lock makes both queue quotas atomic.
            c.execute('SELECT pg_advisory_xact_lock(%s)',(5023961458506702,))
            old=self._row(c.execute('SELECT * FROM canonical_export_jobs WHERE principal_id=%s AND idempotency_sha256=%s',
                (principal_id,key_sha)).fetchone())
            if old:
                require(old['request_sha256']==request_sha and old['api_key_ref']==api_key_ref
                    and old['authorization_sha256']==authorization_sha256,'EXPORT_IDEMPOTENCY_CONFLICT',ExportConflict)
                return old
            counts=c.execute("SELECT count(*) AS all_jobs,count(*) FILTER(WHERE principal_id=%s) AS owned FROM canonical_export_jobs WHERE status IN ('pending','preparing')",(principal_id,)).fetchone()
            require(counts['owned']<owner_limit and counts['all_jobs']<global_limit,'EXPORT_QUEUE_QUOTA')
            row=c.execute('INSERT INTO canonical_export_jobs(job_id,principal_id,api_key_ref,authorization_sha256,idempotency_sha256,request_sha256,request_json) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                (uuid4(),principal_id,api_key_ref,authorization_sha256,key_sha,request_sha,payload)).fetchone()
            job=self._row(row);self._event(c,job,'accepted');return job

    def get(self,job_id,*,principal_id):
        identifier(job_id);identifier(principal_id)
        with self.connection() as c:
            row=c.execute('SELECT * FROM canonical_export_jobs WHERE job_id=%s AND principal_id=%s',(UUID(job_id),principal_id)).fetchone()
            require(row is not None,'EXPORT_JOB_NOT_FOUND');return self._row(row)

    def list_owned(self,principal_id,*,limit=50):
        identifier(principal_id);require(type(limit) is int and 1<=limit<=100,'INVALID_EXPORT_PAGE')
        with self.connection() as c:
            return [self._row(r) for r in c.execute('SELECT * FROM canonical_export_jobs WHERE principal_id=%s ORDER BY created_at DESC,job_id DESC LIMIT %s',(principal_id,limit)).fetchall()]

    def claim(self,worker_id,*,lease_seconds=60):
        require(type(worker_id) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',worker_id),'INVALID_EXPORT_WORKER')
        require(type(lease_seconds) is int and 10<=lease_seconds<=600,'INVALID_EXPORT_LEASE')
        with self.connection() as c:
            row=c.execute("SELECT job_id FROM canonical_export_jobs WHERE status='pending' OR (status='preparing' AND lease_until<=clock_timestamp()) ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
            if not row:return None
            job=self._row(c.execute("UPDATE canonical_export_jobs SET status='preparing',lease_token=%s,lease_owner=%s,lease_until=clock_timestamp()+(%s * interval '1 second'),attempts=attempts+1,updated_at=clock_timestamp() WHERE job_id=%s RETURNING *",(uuid4(),worker_id,lease_seconds,row['job_id'])).fetchone())
            self._event(c,job,'claimed',{'attempt':job['attempts']});return job

    @staticmethod
    def _fence(c,job_id,token):
        identifier(job_id);identifier(token)
        row=c.execute('SELECT *,lease_until>clock_timestamp() AS lease_live FROM canonical_export_jobs WHERE job_id=%s FOR UPDATE',(UUID(job_id),)).fetchone()
        require(row and row['status']=='preparing' and str(row['lease_token'])==token and row['lease_live'],
            'EXPORT_LEASE_LOST',ExportLeaseLost)
        return CanonicalExportJobs._row(row)

    def renew(self,job_id,token,*,lease_seconds=60):
        require(type(lease_seconds) is int and 10<=lease_seconds<=600,'INVALID_EXPORT_LEASE')
        with self.connection() as c:
            self._fence(c,job_id,token)
            return self._row(c.execute("UPDATE canonical_export_jobs SET lease_until=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp() WHERE job_id=%s RETURNING *",(lease_seconds,UUID(job_id))).fetchone())

    def checkpoint(self,job_id,token,step:ExportCheckpoint,*,release_lease=False):
        require(type(release_lease) is bool,'INVALID_EXPORT_LEASE_OPTION')
        body=step.body();body_sha=digest(body)
        with self.connection() as c:
            job=self._fence(c,job_id,token)
            prior=c.execute('SELECT body_sha256,previous_checkpoint FROM canonical_export_checkpoints WHERE job_id=%s AND operation_id=%s',(UUID(job_id),UUID(step.operation_id))).fetchone()
            if prior:
                require(prior['body_sha256']==body_sha and prior['previous_checkpoint']==step.expected_checkpoint,'EXPORT_CHECKPOINT_IDEMPOTENCY_CONFLICT',ExportConflict);return job
            require(job['checkpoint']==step.expected_checkpoint,'EXPORT_CHECKPOINT_CONFLICT',ExportConflict)
            old_phase,new_phase=PHASES.index(job['phase']),PHASES.index(step.phase)
            require(new_phase in {old_phase,old_phase+1},'EXPORT_PHASE_JUMP')
            require(new_phase!=old_phase or step.processed>=job['processed'],'EXPORT_PROGRESS_BACKWARDS')
            if new_phase==old_phase and job['total_relation']=='eq':
                require(step.total_relation=='eq' and step.total==job['total'],'EXPORT_PHASE_TOTAL_CHANGED')
            cut=step.cut if job['cut'] is None else job['cut']
            require(step.cut is None or job['cut'] is None or literal(step.cut,65536)==literal(job['cut'],65536),'EXPORT_CUT_CHANGED')
            require(new_phase<2 or cut is not None,'EXPORT_CUT_REQUIRED')
            for part in step.parts:
                require(step.phase in {'verifying','publishing'},'EXPORT_PART_BEFORE_VERIFICATION')
                old=c.execute('SELECT * FROM canonical_export_parts WHERE job_id=%s AND part_id=%s',(UUID(job_id),UUID(part['part_id']))).fetchone()
                if old:
                    require(all(old[key]==part[key] for key in ['ordinal','file_name','bytes','sha256','verification_sha256'])
                        and old['rows_json']==literal(part['rows'],65536),'EXPORT_PART_CHANGED',ExportConflict)
                else:
                    c.execute('INSERT INTO canonical_export_parts(job_id,part_id,ordinal,file_name,bytes,sha256,verification_sha256,rows_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                        (UUID(job_id),UUID(part['part_id']),part['ordinal'],part['file_name'],part['bytes'],part['sha256'],part['verification_sha256'],literal(part['rows'],65536)))
            if step.complete:
                require(step.phase=='publishing' and step.total_relation=='eq' and step.total==step.processed and step.cursor is None,'EXPORT_NOT_COMPLETE')
                require(c.execute('SELECT count(*) AS n FROM canonical_export_parts WHERE job_id=%s',(UUID(job_id),)).fetchone()['n']>0,'VERIFIED_EXPORT_ARTIFACT_REQUIRED')
            checkpoint=job['checkpoint']+1
            c.execute('INSERT INTO canonical_export_checkpoints(job_id,operation_id,previous_checkpoint,next_checkpoint,body_sha256,body_json) VALUES(%s,%s,%s,%s,%s,%s)',
                (UUID(job_id),UUID(step.operation_id),job['checkpoint'],checkpoint,body_sha,literal(body,262144)))
            updated=self._row(c.execute("UPDATE canonical_export_jobs SET phase=%s,processed=%s,total=%s,total_relation=%s,cursor_json=%s,cut_json=%s,checkpoint=%s,status=%s,updated_at=clock_timestamp(),lease_token=CASE WHEN %s THEN NULL ELSE lease_token END,lease_until=CASE WHEN %s THEN NULL WHEN %s THEN clock_timestamp() ELSE lease_until END,completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,expires_at=CASE WHEN %s THEN clock_timestamp()+interval '7 days' ELSE NULL END WHERE job_id=%s RETURNING *",
                (step.phase,step.processed,step.total,step.total_relation,literal(step.cursor,65536),literal(cut,65536),checkpoint,'completed' if step.complete else 'preparing',step.complete,step.complete,release_lease,step.complete,step.complete,UUID(job_id))).fetchone())
            self._event(c,updated,'completed' if step.complete else 'checkpoint',{'phase':step.phase,'processed':step.processed,'total':step.total});return updated

    def available_parts(self,job_id,*,principal_id,authorization_sha256):
        identifier(job_id);identifier(principal_id);sha(authorization_sha256)
        with self.connection() as c:
            job=c.execute('SELECT *,expires_at>clock_timestamp() AS downloadable FROM canonical_export_jobs WHERE job_id=%s AND principal_id=%s',
                (UUID(job_id),principal_id)).fetchone()
            require(job is not None,'EXPORT_JOB_NOT_FOUND')
            require(job['authorization_sha256']==authorization_sha256,'EXPORT_ACCESS_REVOKED',ExportAccessRevoked)
            require(job['status']=='completed' and job['downloadable'],'EXPORT_FILE_UNAVAILABLE')
            rows=c.execute('SELECT * FROM canonical_export_parts WHERE job_id=%s ORDER BY ordinal',(UUID(job_id),)).fetchall()
            return [{'part_id':str(row['part_id']),'ordinal':row['ordinal'],'file_name':row['file_name'],
                'bytes':row['bytes'],'sha256':row['sha256'],'verification_sha256':row['verification_sha256'],
                'rows':decode(row['rows_json'])} for row in rows]

    def expire_due(self,*,limit=100):
        """Revoke artifact availability; filesystem cleanup is a separate adapter."""
        require(type(limit) is int and 1<=limit<=1000,'INVALID_EXPORT_EXPIRY_BATCH')
        with self.connection() as c:
            selected=c.execute("SELECT job_id FROM canonical_export_jobs WHERE status='completed' AND expires_at<=clock_timestamp() ORDER BY expires_at,job_id FOR UPDATE SKIP LOCKED LIMIT %s",(limit,)).fetchall()
            result=[]
            for row in selected:
                job=self._row(c.execute("UPDATE canonical_export_jobs SET status='expired',updated_at=clock_timestamp() WHERE job_id=%s RETURNING *",(row['job_id'],)).fetchone())
                self._event(c,job,'expired');result.append(job)
            return result

    def cancel(self,job_id,*,principal_id):
        identifier(job_id);identifier(principal_id)
        with self.connection() as c:
            row=c.execute('SELECT * FROM canonical_export_jobs WHERE job_id=%s AND principal_id=%s FOR UPDATE',(UUID(job_id),principal_id)).fetchone()
            require(row is not None,'EXPORT_JOB_NOT_FOUND');job=self._row(row)
            if job['status']=='cancelled':return job
            require(job['status'] not in TERMINAL,'EXPORT_ALREADY_TERMINAL',ExportConflict)
            job=self._row(c.execute("UPDATE canonical_export_jobs SET status='cancelled',lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE job_id=%s RETURNING *",(UUID(job_id),)).fetchone())
            self._event(c,job,'cancelled');return job

    def fail(self,job_id,token,code):
        require(code in {'EXPORT_ACCESS_REVOKED','EXPORT_SOURCE_CURSOR_EXPIRED','EXPORT_STEP_FAILED','EXPORT_ARTIFACT_VERIFICATION_FAILED'},'UNSAFE_EXPORT_ERROR')
        with self.connection() as c:
            self._fence(c,job_id,token)
            job=self._row(c.execute("UPDATE canonical_export_jobs SET status='failed',error_code=%s,lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE job_id=%s RETURNING *",(code,UUID(job_id))).fetchone())
            self._event(c,job,'failed',{'code':code});return job


class CanonicalExportWorker:
    """One bounded unit of work; caller owns scheduling, I/O and rate quotas.

Authorize is a context manager holding the live authorization read lock while
    its returned grant is used; release only after the database checkpoint commits.
A changed/revoked context stops work. The handler must never mutate the database
job or publish paths: return a checkpoint and private, verified artifact receipts.
"""
    def __init__(self,jobs,*,authorize:Callable,step:Callable,verify_part:Callable):
        self.jobs=jobs;self.authorize=authorize;self.step=step;self.verify_part=verify_part

    @staticmethod
    def _authorized(job,context):
        require(type(context) is dict and context.get('active') is True and context.get('can_export') is True
            and context.get('authorization_sha256')==job['authorization_sha256']
            and (job['api_key_ref'] is None or context.get('api_key_active') is True),'EXPORT_ACCESS_REVOKED',ExportAccessRevoked)

    def run_one(self,worker_id,*,lease_seconds=60):
        job=self.jobs.claim(worker_id,lease_seconds=lease_seconds)
        if job is None:return None
        try:
            with self.authorize(job['principal_id'],job['api_key_ref']) as context:self._authorized(job,context)
            step=self.step(job)
            require(isinstance(step,ExportCheckpoint),'INVALID_EXPORT_STEP')
            step.body()
            for part in step.parts:
                require(self.verify_part(job,part) is True,'EXPORT_ARTIFACT_VERIFICATION_FAILED')
            with self.authorize(job['principal_id'],job['api_key_ref']) as context:
                self._authorized(job,context)
                return self.jobs.checkpoint(job['job_id'],job['lease_token'],step,release_lease=True)
        except (ExportRetryableError,psycopg.OperationalError):
            return None # Keep the durable position; retry after the lease expires.
        except ExportLeaseLost:
            return None # Cancellation/new owner prevents any commit or publication.
        except Exception as exc:
            code='EXPORT_ACCESS_REVOKED' if isinstance(exc,ExportAccessRevoked) else ('EXPORT_SOURCE_CURSOR_EXPIRED' if isinstance(exc,ExportSourceExpired) else ('EXPORT_ARTIFACT_VERIFICATION_FAILED' if isinstance(exc,ExportJobError) and str(exc)=='EXPORT_ARTIFACT_VERIFICATION_FAILED' else 'EXPORT_STEP_FAILED'))
            try:return self.jobs.fail(job['job_id'],job['lease_token'],code)
            except ExportLeaseLost:return None
