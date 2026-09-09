"""Transactional catalog, real private PostgreSQL, synthetic accounts only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event
from uuid import uuid4

import psycopg
import pytest

from bigbase.canonical_catalog import (CatalogAdapter, PG_FIELD_CONTRACT, initialize_catalog,
                                      load_definitions, mutate_catalog)
from bigbase.canonical_fields import FIELD_CONTRACT
from bigbase.canonical_store import digest, decode
from bigbase.security import hashed
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store
from test_canonical_fields import custom, definition


@pytest.fixture
def catalog(writable, store):
    initialize_catalog(store)
    client, app, reads, user = writable
    with app.state.store.transaction() as c:
        current = app.state.store.get(c, 'user', user['id'])
        current.update(role='admin', permissions=['read', 'enrich', 'validate', 'admin'])
        app.state.store.put(c, 'user', current)
    return writable


def create(catalog, kind='integer', **changes):
    body = {'id': 'pg-'+uuid4().hex, 'name': 'Definição sintética', 'type': kind, **changes}
    response = catalog[0].post('/api/v1/canonical/fields', json=body, headers={'Idempotency-Key': uuid4().hex})
    assert response.status_code == 200, response.text
    return response.json()['definition']


def patch(catalog, field, changes, key=None, **headers):
    return catalog[0].patch('/api/v1/canonical/fields/'+field['id'], json=changes,
        headers={'Idempotency-Key': key or uuid4().hex, 'If-Match': str(field['version']), **headers})


def pg_item(field, value, **kw):
    result = custom(field, value, **kw)
    result['custom_field']['contract'] = PG_FIELD_CONTRACT
    return result


def catalog_counts(store):
    with store.connection() as c:
        return {name: c.execute('SELECT count(*) AS n FROM '+name).fetchone()['n']
                for name in ['field_catalog', 'field_catalog_versions', 'field_catalog_operations']}


@pytest.mark.parametrize('collection', ['people', 'companies'])
@pytest.mark.parametrize('kind,value,options', [('integer',0,[]), ('integer',123456789012345678901234567890,[]),
    ('decimal',Decimal('12345678901234567890.12345678901234567890'),[]), ('text','',[]),
    ('boolean',False,[]), ('date','2024-02-29',[]), ('enum','A',['A','B']), ('url','https://example.invalid',[]), ('integer',None,[])])
def test_pg_types_preservation_and_receipts(catalog, store, collection, kind, value, options):
    field = create(catalog, kind, options=options, multiple=False)
    body = payload(items=[pg_item(field,value,observed_at='2026-02-01T00:00:00Z',flags={'valid':{'value':False}}), pg_item(field,value,key='alternative')])
    key = uuid4().hex
    response = send(catalog[0],body,key,collection)
    assert response.status_code == 200,response.text
    owner = response.json()['id']; before = counts(store)
    assert send(catalog[0],body,key,collection).json() == {**response.json(),'replayed':True}
    assert counts(store) == before
    rows = store.page_history(owner)['items']; assert len(rows)==3
    for row in rows:
        m=row['metadata']; assert m['catalog_environment']=='postgresql_synthetic'
        assert m['catalog_deployment_id']==store.deployment_info()['deployment_id']
        assert m['field_definition']==field and digest(field)==m['field_definition_sha256']
        assert row['actor_id']==catalog[3]['id'] and row['source_id']=='manual'
    assert rows[0]['input_value']==value and type(rows[0]['input_value']) is type(value)


def test_admin_lifecycle_old_evidence_replay_and_no_implicit_migration(catalog, store):
    client=catalog[0]; field=create(catalog); key=uuid4().hex
    body=payload(items=[pg_item(field,0)]); first=send(client,body,key); owner=first.json()['id']
    original=store.page_history(owner)['items']; receipt_key=uuid4().hex
    renamed=patch(catalog,field,{'name':'Renomeado'},receipt_key); assert renamed.status_code==200
    disabled=patch(catalog,renamed.json()['definition'],{'active':False}).json()['definition']
    before=counts(store)
    assert send(client,payload(items=[pg_item(disabled,1)])).status_code==422
    assert send(client,payload(items=[pg_item(field,1)])).status_code==422
    assert counts(store)==before and store.page_history(owner)['items']==original
    assert send(client,body,key).json()=={**first.json(),'replayed':True}
    assert patch(catalog,field,{'name':'Renomeado'},receipt_key).json()=={**renamed.json(),'replayed':True}
    assert patch(catalog,field,{'name':'Changed'},receipt_key).status_code==409
    assert patch(catalog,disabled,{'type':'text'}).status_code==409
    enabled=patch(catalog,disabled,{'active':True}).json()['definition']
    body.update(expected_version=1,items=[pg_item(enabled,1)])
    assert send(client,body).status_code==200
    assert [r['metadata']['field_definition_version'] for r in store.page_history(owner)['items']]==[1,4]
    h=client.get('/api/v1/canonical/fields/'+field['id']+'/history?limit=2').json()
    assert [r['definition']['version'] for r in h['items']]==[1,2] and h['next_after']==2
    h2=client.get('/api/v1/canonical/fields/'+field['id']+'/history?after=2&limit=2').json()
    assert [r['definition']['version'] for r in h2['items']]==[3,4] and h2['next_after'] is None
    assert all(digest(r['definition'])==r['definition_sha256'] for r in h['items']+h2['items'])


def test_create_replay_generated_id_and_collision(catalog, store):
    client=catalog[0];body={'name':'Generated','type':'text'};key=uuid4().hex
    a=client.post('/api/v1/canonical/fields',json=body,headers={'Idempotency-Key':key});before=catalog_counts(store)
    assert a.status_code==200,a.text
    assert client.post('/api/v1/canonical/fields',json=body,headers={'Idempotency-Key':key}).json()=={**a.json(),'replayed':True}
    assert client.post('/api/v1/canonical/fields',json={**body,'id':a.json()['definition']['id']},headers={'Idempotency-Key':uuid4().hex}).status_code==409
    assert catalog_counts(store)==before


@pytest.mark.parametrize('body', [{'type':'text'},{'active':None},{'active':0},{'name':''},{'unknown':0},{}])
def test_update_invalid_atomic(catalog, store, body):
    field=create(catalog);before=catalog_counts(store)
    assert patch(catalog,field,body).status_code in {409,422}
    assert catalog_counts(store)==before


def test_version_concurrency_and_noop_receipt(catalog, store):
    field=create(catalog)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda n:patch(catalog,field,{'name':'Name '+str(n)}), range(2)))
    assert sorted(r.status_code for r in responses)==[200,409]
    fresh=next(r.json()['definition'] for r in responses if r.status_code==200)
    before=catalog_counts(store);res=patch(catalog,fresh,{'name':fresh['name']});assert res.status_code==200
    after=catalog_counts(store);assert after['field_catalog_versions']==before['field_catalog_versions']
    assert after['field_catalog_operations']==before['field_catalog_operations']+1
    assert catalog[0].patch('/api/v1/canonical/fields/'+field['id'],json={'active':False},headers={'Idempotency-Key':uuid4().hex}).status_code==428
    assert patch(catalog,fresh,{'active':False},**{'If-Match':'true'}).status_code==422


@pytest.mark.parametrize('table',['field_catalog_versions','field_catalog_operations','field_catalog_meta'])
@pytest.mark.parametrize('verb',['UPDATE','DELETE','TRUNCATE'])
def test_catalog_history_database_immutable(catalog,store,table,verb):
    create(catalog)
    column={'field_catalog_versions':'definition_json','field_catalog_operations':'receipt_json','field_catalog_meta':'ddl_sha256'}[table]
    statement= f'UPDATE {table} SET {column}={column}' if verb=='UPDATE' else f'DELETE FROM {table}' if verb=='DELETE' else f'TRUNCATE {table} CASCADE'
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with store.connection() as c:c.execute(statement)


def test_mutation_rollback_and_lost_response(catalog,store,monkeypatch):
    import bigbase.canonical_catalog as module
    field=create(catalog);before=catalog_counts(store);original=CatalogAdapter.put
    def fail(self,c,kind,value):
        original(self,c,kind,value)
        if kind=='field':raise RuntimeError('synthetic failure')
    monkeypatch.setattr(CatalogAdapter,'put',fail)
    with pytest.raises(RuntimeError):mutate_catalog(store,actor_id=catalog[3]['id'],request_key=uuid4().hex,body={'name':'Fail'},field_id=field['id'],if_match='1')
    assert catalog_counts(store)==before
    monkeypatch.setattr(CatalogAdapter,'put',original)
    mutate=module.mutate_catalog
    def lost(*args,**kwargs):mutate(*args,**kwargs);raise ValueError('synthetic lost response')
    monkeypatch.setattr(module,'mutate_catalog',lost);key=uuid4().hex
    assert patch(catalog,field,{'name':'Committed'},key).status_code==503
    monkeypatch.setattr(module,'mutate_catalog',mutate)
    result=patch(catalog,field,{'name':'Committed'},key);assert result.status_code==200 and result.json()['replayed']
    assert result.json()['definition']['version']==2


def test_authorization_stepup_keys_csrf_and_source(catalog, store):
    client,app,reads,user=catalog;field=create(catalog);before=catalog_counts(store)
    with app.state.store.transaction() as c:
        session=app.state.store.get(c,'session',hashed(client.cookies['bigbase_session']))
        saved=session['totp_verified_at'];session['totp_verified_at']=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat();app.state.store.put(c,'session',session)
    assert patch(catalog,field,{'active':False}).json()['detail']['code']=='RECENT_TOTP_REQUIRED'
    with app.state.store.transaction() as c:session['totp_verified_at']=saved;app.state.store.put(c,'session',session)
    csrf=client.headers.pop('X-CSRF-Token')
    assert patch(catalog,field,{'active':False}).status_code==403;client.headers['X-CSRF-Token']=csrf
    token,_=key_for(app,user,scopes=['read','admin','enrich'],sources=['other'])
    assert patch(catalog,field,{'active':False},**{'X-API-Key':token}).json()['detail']['code']=='HUMAN_SESSION_REQUIRED'
    assert send(client,payload(items=[pg_item(field,0)]),headers={'X-API-Key':token}).status_code==403
    with app.state.store.transaction() as c:
        current=app.state.store.get(c,'user',user['id']);current.update(role='user',permissions=['read']);app.state.store.put(c,'user',current)
    assert patch(catalog,field,{'active':False}).status_code==403
    assert client.get('/api/v1/canonical/fields').status_code==200
    assert not client.get('/api/v1/canonical/status').json()['can_administer_catalog']
    assert catalog_counts(store)==before


def test_local_and_pg_same_id_isolation_and_no_rebinding(catalog,store):
    client=catalog[0];local=definition(catalog,'text');field=create(catalog,id=local['id']);before=counts(store)
    assert send(client,payload(items=[pg_item(field,'text')])).status_code==422
    assert send(client,payload(items=[custom(local,0)])).status_code==422
    assert counts(store)==before
    body=payload(items=[custom(local,'old')]);owner=send(client,body).json()['id']
    body.update(expected_version=1,items=[pg_item(field,0)])
    assert send(client,body).status_code==422
    assert store.page_history(owner)['items'][0]['metadata']['catalog_environment']=='local_synthetic'
    body['items'][0]['key']='new-item';assert send(client,body).status_code==200
    assert [r['metadata']['catalog_environment'] for r in store.page_history(owner)['items']]==['local_synthetic','postgresql_synthetic']


def test_unknown_reference_scope_and_transaction_rollback(catalog,store):
    client=catalog[0];field={'id':'unknown-'+uuid4().hex}
    body=payload(items=[pg_item(field,{'null':None,'false':False,'zero':0,'empty':[]})]);owner=send(client,body).json()['id']
    rows=store.page_history(owner)['items'];assert len(rows)==4
    assert all(r['metadata']['field_definition'] is None for r in rows)
    ref=create(catalog,'reference',scope='person')
    result=send(client,payload(items=[pg_item(ref,{'entity_type':'person','id':owner})]));assert result.status_code==200,result.text
    before=counts(store)
    for collection,value in [('companies',{'entity_type':'person','id':owner}),('people',{'entity_type':'company','id':owner}),('people',{'entity_type':'person','id':str(uuid4())})]:
        assert send(client,payload(items=[payload()['items'][0],pg_item(ref,value)]),collection=collection).status_code==422
    assert counts(store)==before


@pytest.mark.parametrize('unknown',[False,True])
def test_acceptance_holds_catalog_lock_until_observation_commit(catalog,store,monkeypatch,unknown):
    import bigbase.canonical_fields as module
    field={'id':'pending-'+uuid4().hex} if unknown else create(catalog)
    entered,release=Event(),Event();original=module.load_definitions
    def held(c,ids):
        result=original(c,ids);entered.set();assert release.wait(10);return result
    monkeypatch.setattr(module,'load_definitions',held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing=pool.submit(send,catalog[0],payload(items=[pg_item(field,0)]))
        assert entered.wait(10)
        changing=pool.submit(mutate_catalog,store,actor_id=catalog[3]['id'],request_key=uuid4().hex,
            body={'id':field['id'],'name':'Later','type':'integer'} if unknown else {'active':False},
            **({} if unknown else {'field_id':field['id'],'if_match':'1'}))
        try:
            # A separate connection cannot obtain the exact field lock while
            # the enrichment transaction is between validation and insertion.
            with store.connection() as c:
                lock=store._lock_key([store.schema,'field-catalog',field['id']])
                assert c.execute('SELECT pg_try_advisory_xact_lock(%s) AS ok',(lock,)).fetchone()['ok'] is False
        finally:release.set()
        response=writing.result(timeout=15);changed=changing.result(timeout=15)
    assert response.status_code==200,response.text
    row=store.page_history(response.json()['id'])['items'][0]
    assert row['metadata']['field_definition_version'] == (None if unknown else 1)
    assert changed['definition']['version']==(1 if unknown else 2)
    assert send(catalog[0],payload(items=[pg_item(field,1)])).status_code==422


def test_list_pages_no_silent_truncation(catalog):
    prefix='zz-'+uuid4().hex
    for i in range(3):create(catalog,id=prefix+str(i))
    page=catalog[0].get('/api/v1/canonical/fields',params={'after':prefix,'limit':2}).json()
    assert [r['id'] for r in page['items']]==[prefix+'0',prefix+'1'] and page['next_after']==prefix+'1'
    page=catalog[0].get('/api/v1/canonical/fields',params={'after':page['next_after'],'limit':1}).json()
    assert page['items'][0]['id']==prefix+'2'
    assert catalog[0].get('/api/v1/canonical/fields?limit=101').status_code==422


def test_runtime_does_not_initialize_extension(writable,store):
    # A distinct base-only fixture preserves its original schema hash.
    from bigbase.canonical_store import CanonicalStore
    from psycopg import sql
    separate=CanonicalStore(store.dsn,'cbtest_'+uuid4().hex);separate.initialize()
    try:
        with separate.connection() as c:
            assert c.execute("SELECT to_regclass('field_catalog_meta') AS t").fetchone()['t'] is None
        with pytest.raises(psycopg.errors.UndefinedTable):
            with separate.connection() as c:load_definitions(c,['unknown'])
        with separate.connection() as c:assert c.execute("SELECT to_regclass('field_catalog_meta') AS t").fetchone()['t'] is None
    finally:
        with separate.connection() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(separate.schema)))
