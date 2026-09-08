"""Durable, bounded import jobs for the isolated single-process development store.

Each source entry and its result remain stored. A row's entity mutation, result,
checkpoint and audit event commit together; restarting a worker cannot replay a
committed observation. This module does not connect to production data sources.
"""
from copy import deepcopy
from datetime import datetime, timezone
import logging

from fastapi import HTTPException
from pydantic import ValidationError

from .domain import Enrichment, canonical, now, timestamp, uid
from .enrichment import enrich_entity


MAX_ENTRIES=1000
MAX_BYTES=2*1024*1024
RUNNABLE={'pending','processing'}
TERMINAL={'completed','completed_with_errors','failed','cancelled'}
PUBLIC_FIELDS=('id','user_id','owner_id','name','status','total','processed','cursor',
               'succeeded','failed_count','progress_percent','created_at','updated_at',
               'started_at','completed_at','failed_at','cancelled_at','error','resume_history')
logger=logging.getLogger(__name__)


class _PrincipalUnavailable(Exception):
    def __init__(self,code,message,status_code=403):
        self.error={'code':code,'message':message,'status_code':status_code}


def _json_types(value):
    if isinstance(value,dict):
        if any(type(key) is not str for key in value):raise ValueError('JSON object key')
        for child in value.values():_json_types(child)
    elif isinstance(value,list):
        for child in value:_json_types(child)
    elif value is not None and type(value) not in {str,bool,int,float}:raise ValueError('JSON value')


class Importer:
    def __init__(self,store):self.store=store

    def prepare(self,entries,owner_id,name,api_key_id=None):
        if not isinstance(entries,list) or not 1<=len(entries)<=MAX_ENTRIES:
            raise HTTPException(422,'Importação local exige de 1 a 1.000 entradas')
        if not isinstance(name,str) or not name.strip() or len(name)>160:
            raise HTTPException(422,'Nome da importação deve ter de 1 a 160 caracteres')
        if not isinstance(owner_id,str) or not owner_id or (api_key_id is not None and (not isinstance(api_key_id,str) or not api_key_id)):
            raise HTTPException(422,'Principal da importação inválido')
        try:
            _json_types(entries)
            size=len(canonical(entries).encode('utf-8'))
        except (ValueError,TypeError,RecursionError,UnicodeError):
            raise HTTPException(422,'Entradas precisam conter valores JSON válidos') from None
        if size>MAX_BYTES:raise HTTPException(413,'Importação local excede 2 MiB')
        at=now()
        return {'id':uid(),'user_id':owner_id,'owner_id':owner_id,'api_key_id':api_key_id,
                'name':name.strip(),'status':'pending','entries':deepcopy(entries),'results':[],
                'cursor':0,'processed':0,'total':len(entries),'succeeded':0,'failed_count':0,
                'progress_percent':0,'created_at':at,'updated_at':at,'error':None}

    def public(self,job):return {key:deepcopy(job[key]) for key in PUBLIC_FIELDS if key in job}

    def result_detail(self,job):
        recorded={result['index']:result for result in job['results']}
        remaining='not_processed' if job['status'] in TERMINAL else 'pending'
        items=[deepcopy(recorded.get(index,{'index':index,'input_id':str(index+1),'status':remaining,'error':None})) for index in range(job['total'])]
        return {'job':self.public(job),'items':items,'total':job['total']}

    def _principal(self,c,job):
        owner_id=job['owner_id'];user=self.store.get(c,'user',owner_id)
        if not user or user.get('active') is not True or user.get('otp_enabled') is not True:
            raise _PrincipalUnavailable('principal_unavailable','Usuário desativado ou autenticação indisponível.',401)
        if not isinstance(user.get('permissions'),list) or 'enrich' not in user['permissions']:
            raise _PrincipalUnavailable('permission_revoked','Permissão de enriquecimento indisponível.')
        key=None
        if job.get('api_key_id'):
            key=self.store.get(c,'api_key',job['api_key_id'])
            if not key or key.get('user_id')!=owner_id or key.get('active') is not True:
                raise _PrincipalUnavailable('api_key_revoked','Chave de API revogada ou indisponível.',401)
            try:expires=timestamp(key.get('expires_at'))
            except (ValueError,TypeError):expires=None
            if expires is None or expires<=datetime.now(timezone.utc):
                raise _PrincipalUnavailable('api_key_expired','Chave de API expirada.',401)
            if not isinstance(key.get('scopes'),list) or 'enrich' not in key['scopes']:
                raise _PrincipalUnavailable('api_key_scope_revoked','Chave sem permissão de enriquecimento.')
        return user,key

    def _source(self,c,body,key):
        source=self.store.get(c,'source',body.source_id)
        if not source or source.get('active') is not True:
            raise HTTPException(422,'Origem inexistente ou inativa')
        if key and (not isinstance(key.get('sources'),list) or body.source_id not in key['sources']):
            raise HTTPException(403,'Origem não permitida para esta chave')

    def _audit(self,c,job,action,**details):
        event={'id':uid(),'actor_id':job['owner_id'],'action':action,'target_id':job['id'],'at':now(),**details}
        if job.get('api_key_id'):
            key=self.store.get(c,'api_key',job['api_key_id'])
            if key and key.get('public_id'):event['api_key_public_id']=key['public_id']
        self.store.event(c,event)

    def _stop(self,c,job,error):
        job.update(status='failed',error=error,failed_at=now(),updated_at=now())
        self.store.put(c,'import_job',job)
        self._audit(c,job,'import_stopped',error_code=error['code'])

    def _error(self,exc):
        if isinstance(exc,ValidationError):
            return {'code':'invalid_enrichment','message':'Entrada incompatível com o formato de enriquecimento; revise campos, tipos e datas.','status_code':422,
                    'validation_codes':sorted({error['type'] for error in exc.errors(include_input=False,include_context=False)})}
        if isinstance(exc,HTTPException):
            messages={404:'Cadastro de destino não encontrado.',409:'Conflito de identidade ou versão; revisão necessária.',
                      401:'Operação não autorizada.',403:'Origem ou operação não permitida.',422:'Entrada não passou pela validação cadastral.'}
            return {'code':'entry_rejected','message':messages.get(exc.status_code,'Entrada não pôde ser processada.'),'status_code':exc.status_code}
        return {'code':'entry_processing_failed','message':'Falha ao processar esta entrada; nenhuma alteração dela foi mantida.','status_code':500}

    def run(self,job_id):
        try:
            while True:
                with self.store.transaction() as c:
                    job=self.store.get(c,'import_job',job_id)
                    if not job or job['status'] not in RUNNABLE:return
                    if (job['total']!=len(job['entries']) or job['processed']!=len(job['results'])
                        or job['cursor']!=job['processed'] or job['succeeded']+job['failed_count']!=job['processed']
                        or [r.get('index') for r in job['results']]!=list(range(job['processed']))):
                        self._stop(c,job,{'code':'checkpoint_inconsistent','message':'Checkpoint inconsistente; revisão necessária antes de retomar.','status_code':500});return
                    try:user,key=self._principal(c,job)
                    except _PrincipalUnavailable as exc:
                        self._stop(c,job,exc.error);return
                    index=job['cursor']
                    if index>=job['total']:
                        job.update(status='completed_with_errors' if job['failed_count'] else 'completed',progress_percent=100,completed_at=now(),updated_at=now())
                        self.store.put(c,'import_job',job);return
                    job.update(status='processing',started_at=job.get('started_at') or now())
                    result={'index':index,'input_id':str(index+1),'error':None}
                    c.execute('SAVEPOINT import_entry')
                    try:
                        body=Enrichment.model_validate(job['entries'][index])
                        self._source(c,body,key)
                        entity=enrich_entity(self.store,c,body,body.entity_type,user['id'])
                        result.update(status='succeeded',entity_id=entity['id'],entity_version=entity['version'])
                    except Exception as exc:
                        c.execute('ROLLBACK TO SAVEPOINT import_entry')
                        result.update(status='failed',error=self._error(exc))
                    finally:c.execute('RELEASE SAVEPOINT import_entry')
                    result['processed_at']=now();job['results'].append(result)
                    job['processed']+=1;job['cursor']=job['processed']
                    job['succeeded' if result['status']=='succeeded' else 'failed_count']+=1
                    job.update(progress_percent=int(100*job['processed']/job['total']),updated_at=now())
                    if job['processed']==job['total']:
                        job.update(status='completed_with_errors' if job['failed_count'] else 'completed',completed_at=now())
                    self._audit(c,job,'import_entry',entry_index=index,result=result['status'],entity_id=result.get('entity_id'),error_code=(result.get('error') or {}).get('code'))
                    self.store.put(c,'import_job',job)
        except Exception:
            # A storage/commit failure rolls back the complete row transaction.
            # Do not leak exception messages, which may contain a source value.
            try:
                with self.store.transaction() as c:
                    job=self.store.get(c,'import_job',job_id)
                    if job and job['status'] in RUNNABLE:
                        self._stop(c,job,{'code':'import_processing_failed','message':'Importação interrompida; checkpoint anterior preservado.','status_code':500})
            except Exception:logger.error('Falha ao persistir interrupção de importação local.')
