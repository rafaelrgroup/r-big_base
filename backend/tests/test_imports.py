"""Synthetic-only tests of local import checkpoints and source authorization."""
from contextlib import contextmanager
from copy import deepcopy
import json

from fastapi import HTTPException
import pytest

import bigbase.imports as import_module
from bigbase.domain import fingerprint
from bigbase.imports import Importer
from bigbase.store import Store


OWNER='synthetic-import-owner'
KEY_ID='0'*64


@pytest.fixture
def env(tmp_path):
    store=Store(tmp_path/'imports.sqlite')
    with store.transaction() as c:
        store.put(c,'user',{'id':OWNER,'active':True,'otp_enabled':True,'permissions':['read','enrich']})
        for source in ['manual','synthetic-other']:
            store.put(c,'source',{'id':source,'active':True})
        store.put(c,'api_key',{'id':KEY_ID,'public_id':'synthetic-public-key','user_id':OWNER,'active':True,'scopes':['enrich'],
                               'sources':['manual','synthetic-other'],'expires_at':'2099-01-01T00:00:00Z'})
    return store,Importer(store)


def entry(name='Pessoa Sintética Importada',**extra):
    return {'source_id':'manual','observed_at':'2026-01-01T00:00:00Z','items':[{'kind':'identity','value':{'name':name}}],**extra}


def persist(env,entries,key=None):
    store,importer=env;job=importer.prepare(entries,OWNER,'Importação sintética',key)
    with store.transaction() as c:store.put(c,'import_job',job)
    return job


def read_job(env,job):
    with env[0].transaction() as c:return env[0].get(c,'import_job',job['id'])


def test_prepare_retains_original_rows_and_public_never_discloses_them(env):
    _,importer=env
    entries=[entry('PRIVATE_SYNTHETIC_NAME'),None,'PRIVATE_SYNTHETIC_INVALID_ROW']
    expected=deepcopy(entries)
    job=importer.prepare(entries,OWNER,'Importação sintética',KEY_ID)
    entries[0]['items'][0]['value']['name']='Changed only in caller'
    assert job['entries']==expected
    assert job['total']==3 and job['processed']==0
    assert job['results']==[] and job['status']=='pending'
    public=json.dumps(importer.public(job))
    assert 'PRIVATE_SYNTHETIC' not in public and KEY_ID not in public
    assert 'entries' not in importer.public(job) and 'results' not in importer.public(job)
    detail=importer.result_detail(job)
    assert [item['status'] for item in detail['items']]==['pending']*3
    assert 'PRIVATE_SYNTHETIC' not in json.dumps(detail)


@pytest.mark.parametrize('entries',[None,{},[],[{}]*1001])
def test_prepare_rejects_invalid_envelope_or_entry_count(env,entries):
    with pytest.raises(HTTPException) as exc:env[1].prepare(entries,OWNER,'Sintético')
    assert exc.value.status_code==422


@pytest.mark.parametrize('name',[None,0,'','   ','x'*161])
def test_prepare_requires_bounded_nonempty_name(env,name):
    with pytest.raises(HTTPException) as exc:env[1].prepare([entry()],OWNER,name)
    assert exc.value.status_code==422


def test_prepare_checks_actual_utf8_bytes_and_non_json_values(env):
    with pytest.raises(HTTPException) as exc:env[1].prepare([entry('á'*1048576)],OWNER,'Sintético')
    assert exc.value.status_code==413
    for bad in [float('nan'),object(),{1:'non-text-key'}]:
        with pytest.raises(HTTPException) as exc:env[1].prepare([bad],OWNER,'Sintético')
        assert exc.value.status_code==422


def test_mixed_rows_preserve_unicode_null_false_zero_and_unknown_fields(env):
    store,importer=env
    value={'coleção':[None,False,0,'ação 🚀'],'objeto':{},'lista':[]}
    valid=entry(items=[{'kind':'identity','value':{'name':'Pessoa Sintética Ação'}},
                       {'kind':'custom','value':{'field_id':'synthetic-unknown','value':value}},
                       {'kind':'phone','value':{'number':'0123'},'flags':{'is_whatsapp':None,'valid':False}}])
    extra=entry();extra['PRIVATE_UNRECOGNIZED_FIELD']='PRIVATE_INPUT_VALUE'
    entries=[valid,extra,None,entry('Empresa Sintética',entity_type='company')]
    job=persist(env,entries);importer.run(job['id']);result=read_job(env,job)
    assert result['entries']==entries
    assert result['status']=='completed_with_errors'
    assert (result['processed'],result['cursor'],result['succeeded'],result['failed_count'],result['progress_percent'])==(4,4,2,2,100)
    assert result['results'][1]['error']['status_code']==422
    assert 'extra_forbidden' in result['results'][1]['error']['validation_codes']
    assert result['results'][2]['error']['status_code']==422
    assert 'PRIVATE_' not in json.dumps(importer.result_detail(result))
    with store.transaction() as c:
        entities=store.all(c,'entity')
        audits=[json.loads(row[0]) for row in c.execute('SELECT body FROM events')]
    assert len(entities)==2 and {entity['entity_type'] for entity in entities}=={'person','company'}
    person=next(entity for entity in entities if entity['entity_type']=='person')
    custom=next(item for item in person['items'] if item['kind']=='custom')
    assert fingerprint(custom['value']['value'])==fingerprint(value)
    assert custom['value']['value']['coleção'][1] is False
    assert type(custom['value']['value']['coleção'][2]) is int
    assert any(fingerprint(o['input_value'])==fingerprint(value) for o in person['observations'])
    assert len(audits)==4 and all(event['actor_id']==OWNER for event in audits)
    assert 'PRIVATE_' not in json.dumps(audits)


def test_successful_checkpoint_prevents_repeat_observations_on_double_run(env):
    store,importer=env
    job=persist(env,[entry(external_id='synthetic-stable-source')]);importer.run(job['id'])
    with store.transaction() as c:
        before=store.all(c,'entity');events=c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    importer.run(job['id']);Importer(store).run(job['id'])
    with store.transaction() as c:
        assert store.all(c,'entity')==before
        assert c.execute('SELECT COUNT(*) FROM events').fetchone()[0]==events


def test_restart_after_partial_checkpoint_rolls_back_uncommitted_entry(env,monkeypatch):
    store,importer=env
    entries=[entry(name,external_id='synthetic-restart-owner',observed_at=date) for name,date in [
        ('Sintético Primeiro','2026-01-01T00:00:00Z'),('Sintético Segundo','2026-02-01T00:00:00Z'),('Sintético Terceiro','2026-03-01T00:00:00Z')]]
    job=persist(env,entries);real=import_module.enrich_entity;calls=0
    def crash_after_write(*args):
        nonlocal calls
        result=real(*args);calls+=1
        if calls==2:raise KeyboardInterrupt('simulated worker stop')
        return result
    monkeypatch.setattr(import_module,'enrich_entity',crash_after_write)
    with pytest.raises(KeyboardInterrupt):importer.run(job['id'])
    partial=read_job(env,job)
    assert partial['status']=='processing' and partial['processed']==1 and len(partial['results'])==1
    with store.transaction() as c:
        saved=store.all(c,'entity')[0]
        assert saved['version']==1 and saved['name']=='Sintético Primeiro'
        first_observation=saved['observations'][0]['id']
    monkeypatch.setattr(import_module,'enrich_entity',real)
    Importer(store).run(job['id'])
    completed=read_job(env,job)
    assert completed['status']=='completed' and completed['processed']==3
    with store.transaction() as c:
        entities=store.all(c,'entity');assert len(entities)==1
        assert entities[0]['version']==3 and entities[0]['name']=='Sintético Terceiro'
        assert len(entities[0]['observations'])==3
        assert entities[0]['observations'][0]['id']==first_observation


def test_row_failure_rolls_back_partial_entity_and_audit_then_continues(env,monkeypatch):
    store,importer=env
    job=persist(env,[entry('Sintético Primeiro'),entry('Sintético Rejeitado',external_id='synthetic-fail'),entry('Sintético Terceiro')])
    real=import_module.enrich_entity
    def fail_after_write(store,c,body,*args):
        result=real(store,c,body,*args)
        if body.external_id=='synthetic-fail':
            store.event(c,{'PRIVATE_PARTIAL_EVENT':'must roll back'})
            raise HTTPException(422,'PRIVATE_SOURCE_DATA must not leak')
        return result
    monkeypatch.setattr(import_module,'enrich_entity',fail_after_write)
    importer.run(job['id']);result=read_job(env,job)
    assert result['status']=='completed_with_errors' and result['succeeded']==2 and result['failed_count']==1
    assert 'PRIVATE_' not in json.dumps(importer.result_detail(result))
    with store.transaction() as c:
        assert {entity['name'] for entity in store.all(c,'entity')}=={'Sintético Primeiro','Sintético Terceiro'}
        audit=[json.loads(row[0]) for row in c.execute('SELECT body FROM events')]
    assert len(audit)==3 and 'PRIVATE_' not in json.dumps(audit)


@pytest.mark.parametrize(('change','error_code'),[
    ('user_inactive','principal_unavailable'),('otp_reset','principal_unavailable'),
    ('permission_removed','permission_revoked'),('key_revoked','api_key_revoked'),
    ('key_expired','api_key_expired'),('key_scope_removed','api_key_scope_revoked'),
])
def test_principal_is_revalidated_before_each_entry(env,monkeypatch,change,error_code):
    store,importer=env;job=persist(env,[entry('Sintético Primeiro'),entry('Sintético Não Processado')],KEY_ID)
    real=import_module.enrich_entity
    def first_then_revoke(store,c,*args):
        entity=real(store,c,*args)
        if change in {'user_inactive','otp_reset','permission_removed'}:
            user=store.get(c,'user',OWNER)
            if change=='user_inactive':user['active']=False
            elif change=='otp_reset':user['otp_enabled']=False
            else:user['permissions']=[]
            store.put(c,'user',user)
        else:
            key=store.get(c,'api_key',KEY_ID)
            if change=='key_revoked':key['active']=False
            elif change=='key_expired':key['expires_at']='2000-01-01T00:00:00Z'
            else:key['scopes']=[]
            store.put(c,'api_key',key)
        return entity
    monkeypatch.setattr(import_module,'enrich_entity',first_then_revoke)
    importer.run(job['id']);result=read_job(env,job)
    assert result['status']=='failed' and result['error']['code']==error_code
    assert result['processed']==1 and result['progress_percent']==50
    assert importer.result_detail(result)['items'][1]['status']=='not_processed'
    importer.run(job['id'])
    with store.transaction() as c:
        assert len(store.all(c,'entity'))==1
        audit=[json.loads(row[0]) for row in c.execute('SELECT body FROM events')]
    assert all(event['api_key_public_id']=='synthetic-public-key' for event in audit)
    assert KEY_ID not in json.dumps(audit)


@pytest.mark.parametrize('mode',['source_inactive','source_scope_removed'])
def test_source_authorization_is_rechecked_and_other_rows_continue(env,monkeypatch,mode):
    store,importer=env;job=persist(env,[entry('Sintético Primeiro'),entry('Sintético Bloqueado'),entry('Sintético Outro',source_id='synthetic-other')],KEY_ID)
    real=import_module.enrich_entity
    def deactivate_source(store,c,*args):
        entity=real(store,c,*args)
        if mode=='source_inactive':
            source=store.get(c,'source','manual');source['active']=False;store.put(c,'source',source)
        else:
            key=store.get(c,'api_key',KEY_ID);key['sources']=['synthetic-other'];store.put(c,'api_key',key)
        return entity
    monkeypatch.setattr(import_module,'enrich_entity',deactivate_source)
    importer.run(job['id']);result=read_job(env,job)
    assert result['status']=='completed_with_errors' and result['succeeded']==2
    assert result['results'][1]['error']['status_code']==(422 if mode=='source_inactive' else 403)
    assert result['results'][2]['status']=='succeeded'


def test_cancellation_between_commits_is_observed_without_replay(env,monkeypatch):
    store,importer=env;job=persist(env,[entry('Sintético Primeiro'),entry('Sintético Cancelado')])
    transaction=store.transaction;cancelled=False
    @contextmanager
    def cancel_after_checkpoint():
        nonlocal cancelled
        with transaction() as c:yield c
        if not cancelled:
            with transaction() as c:
                saved=store.get(c,'import_job',job['id'])
                if saved['processed']==1 and saved['status']=='processing':
                    cancelled=True;saved['status']='cancelled';store.put(c,'import_job',saved)
    monkeypatch.setattr(store,'transaction',cancel_after_checkpoint)
    importer.run(job['id']);result=read_job(env,job)
    assert result['status']=='cancelled' and result['processed']==1
    assert importer.result_detail(result)['items'][1]['status']=='not_processed'
    Importer(store).run(job['id'])
    with transaction() as c:assert len(store.all(c,'entity'))==1
