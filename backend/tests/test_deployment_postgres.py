"""Release integration in a SEPARATE validation database, never the migration DB."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import hashlib
import json
import os

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.canonical_catalog import DDL,PG_FIELD_CONTRACT,initialize_catalog
from bigbase.canonical_store import CanonicalStore,decode,json_text
from bigbase.deployment import DeploymentIdentity,DeploymentStore,DeploymentReads,DeploymentRuntime,private_text
from bigbase.security import hashed
from test_deployment_runtime import setup_control,FakeRedis,login


@pytest.fixture
def postgres(tmp_path,monkeypatch,request):
    path=os.environ.get('BIGBASE_RELEASE_VALIDATION_DSN_FILE')
    if not path:pytest.skip('Explicit isolated release-validation DSN file required')
    dsn=private_text(path);values=conninfo_to_dict(dsn)
    assert values.get('host')=='127.0.0.1' and values.get('port')=='15432'
    assert values.get('dbname')=='bigbase_release_validation' and not values.get('hostaddr')
    schema='release_test_'+uuid4().hex
    bootstrap=CanonicalStore(dsn,schema)
    info=bootstrap.initialize(environment='staging')
    def cleanup():
        with psycopg.connect(dsn) as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    request.addfinalizer(cleanup)
    with bootstrap.connection() as c:
        c.execute(DDL.read_text())
        c.execute('INSERT INTO field_catalog_meta(deployment_id,ddl_sha256) VALUES(%s,%s)',
                  (info['deployment_id'],hashlib.sha256(DDL.read_bytes()).hexdigest()))
    expected=DeploymentIdentity(info['deployment_id'],'staging','bigbase_release_validation',schema,15432)
    store=DeploymentStore(dsn,expected);reads=DeploymentReads(store,writes_enabled=True)
    root=tmp_path/'control';_,user,secret=setup_control(root)
    runtime=DeploymentRuntime(expected,root,'redis://127.0.0.1:16379/0',('panel.example',))
    monkeypatch.setenv('BIGBASE_ENV','staging');monkeypatch.delenv('BIGBASE_LOCAL_HTTP',raising=False)
    monkeypatch.setattr('bigbase.api.RedisLimiter',FakeRedis)
    app=create_app(canonical_reads=reads,deployment=runtime)
    with TestClient(app,base_url='https://panel.example') as client:
        login(client,secret)
        yield client,app,store,reads,user


def payload():
    return {'source_id':'manual','source_record_id':'release-validation-'+uuid4().hex,'expected_version':0,
            'items':[{'kind':'identity','key':'identity','fields':[{'path':'name','value':'SYNTHETIC RELEASE TEST'}]}]}


def send(c,body,key=None):
    return c.post('/api/v1/canonical/people/enrich',content=json_text(body),
                  headers={'Content-Type':'application/json','Idempotency-Key':key or uuid4().hex})


def test_real_pg_identity_enrich_lookup_history_precision_and_replay(postgres):
    c,app,store,reads,user=postgres
    assert c.get('/api/v1/health').json()['canonical_connected']
    body=payload();values=[False,0,None,'',Decimal('12345678901234567890.12345678901234567890'),'nul\x00surrogate\ud800']
    body['items']=[{'kind':'custom','key':'preserved','fields':[{'path':'v'+str(i),'value':v} for i,v in enumerate(values)]}]
    key=uuid4().hex;result=send(c,body,key)
    assert result.status_code==200,result.text
    receipt=result.json();assert receipt['environment']=='staging' and not receipt['production_connected']
    assert send(c,body,key).json()=={**receipt,'replayed':True}
    lookup=c.post('/api/v1/canonical/people/lookup',json={'source_id':'manual','source_record_id':body['source_record_id']})
    assert lookup.status_code==200 and lookup.json()['id']==receipt['id']
    rows=store.page_history(receipt['id'])['items']
    assert len(rows)==len(values)
    assert all(type(row['input_value']) is type(v) and row['input_value']==v for row,v in zip(rows,values))
    assert all(row['source_id']=='manual' and row['actor_id']==user['id'] for row in rows)
    with app.state.store.transaction() as conn:assert not app.state.store.all(conn,'entity')
    assert reads.cleanup_read_cursors()>=0


def test_real_catalog_definition_binding_version_and_no_local_fallback(postgres):
    c,app,store,reads,user=postgres
    r=c.post('/api/v1/canonical/fields',json={'id':'release-field','name':'Synthetic boolean','type':'boolean'},headers={'Idempotency-Key':uuid4().hex})
    assert r.status_code==200,r.text
    field=r.json()['definition'];assert r.json()['catalog_environment']=='postgresql_staging'
    body=payload();body['items']=[{'kind':'custom','key':'bool','custom_field':{'contract':PG_FIELD_CONTRACT,'field_id':field['id'],'version':1},'fields':[{'path':'value','value':False}]}]
    saved=send(c,body);assert saved.status_code==200,saved.text
    row=store.page_history(saved.json()['id'])['items'][0]
    assert row['input_value'] is False and row['metadata']['catalog_environment']=='postgresql_staging'
    assert row['metadata']['field_definition']==field
    changed=c.patch('/api/v1/canonical/fields/'+field['id'],json={'name':'Renamed'},headers={'Idempotency-Key':uuid4().hex,'If-Match':'1'})
    assert changed.status_code==200 and changed.json()['definition']['version']==2
    assert send(c,body).status_code==422
    history=c.get('/api/v1/canonical/fields/'+field['id']+'/history').json()
    assert [r['definition']['version'] for r in history['items']]==[1,2]
    legacy=deepcopy(body);legacy['items'][0]['custom_field']['contract']='canonical-custom-field-2026-09-09.1'
    assert send(c,legacy).json()['detail']['code']=='POSTGRESQL_CATALOG_REQUIRED'
    with pytest.raises(ValueError):initialize_catalog(store)


@pytest.mark.parametrize('kind,value', [('boolean', False), ('integer', 0),
    ('decimal', '12345678901234567890.1234567890123456789'), ('date', '2024-02-29')])
def test_deployed_catalog_values_keep_projection_binding_after_catalog_update(postgres, kind, value):
    from bigbase.canonical_search import build_projection, build_query, custom_selector
    c, app, store, reads, user = postgres
    response = c.post('/api/v1/canonical/fields', json={'id': 'projection-fixture', 'name': 'Synthetic projection', 'type': kind},
                      headers={'Idempotency-Key': uuid4().hex})
    assert response.status_code == 200
    definition = response.json()['definition']
    body = payload()
    body['items'] = [{'kind': 'custom', 'key': 'projection',
                     'custom_field': {'contract': PG_FIELD_CONTRACT, 'field_id': definition['id'], 'version': 1},
                     'fields': [{'path': 'value', 'value': value}]}]
    saved = send(c, body)
    assert saved.status_code == 200
    entity = store.get_entity(saved.json()['id'])
    document = build_projection(entity)
    field = next(field for item in entity['items'] for field in item['fields'])
    selector = custom_selector(field['metadata'])
    assert selector['deployment_id'] == reads.deployment_id
    compiled = build_query({'kind': 'custom', 'field': 'value', 'value': value, 'custom': selector})
    projected = next(field for item in document['items'] for field in item['fields'] if 'custom_key' in field)
    assert projected['custom_key'] in json.dumps(compiled)
    changed = c.patch('/api/v1/canonical/fields/' + definition['id'], json={'name': 'Renamed synthetic field', 'active': False},
                      headers={'Idempotency-Key': uuid4().hex, 'If-Match': '1'})
    assert changed.status_code == 200
    assert build_projection(store.get_entity(saved.json()['id'])) == document


def test_real_flags_preserve_tristate_and_historical_value(postgres):
    c,app,store,reads,user=postgres
    body=payload();saved=send(c,body).json();row=store.page_history(saved['id'])['items'][0]
    flags={'source_id':'manual','expected_version':1,'field_path':row['field_path'],
           'value_observation_id':row['observation_id'],'flags':{'valid':{'value':False,'reason':'Synthetic invalidation test'},'is_whatsapp':{'value':None}}}
    url=f"/api/v1/canonical/people/{saved['id']}/items/{row['item_id']}/flags"
    result=c.patch(url,json=flags,headers={'Idempotency-Key':uuid4().hex})
    assert result.status_code==200,result.text
    assert result.json()['environment']=='staging'
    history=store.page_history(saved['id'])['items']
    assert len(history)==3 and history[0]['input_value']=='SYNTHETIC RELEASE TEST'
    assert history[1]['input_value'] is False and history[2]['input_value'] is None


def test_pg_api_keys_require_scopes_and_source_and_can_query_without_cookie(postgres):
    c,app,store,reads,user=postgres
    body=payload();receipt=send(c,body).json()
    token='synthetic-release-'+uuid4().hex
    with app.state.store.transaction() as conn:
        app.state.store.put(conn,'api_key',{'id':hashed(token),'public_id':uuid4().hex,'user_id':user['id'],
          'name':'Synthetic release validation','active':True,'scopes':['read'],'sources':['manual'],
          'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    c.cookies.clear();c.headers.pop('X-CSRF-Token')
    c.headers['X-API-Key']=token
    assert c.get('/api/v1/canonical/people/'+receipt['id']).status_code==200
    assert send(c,payload()).status_code==403


def test_real_pg_destination_divergence_fails_closed_without_fixture(postgres):
    c,app,store,reads,user=postgres
    actual=store.identity
    store.identity=DeploymentIdentity(str(uuid4()),actual.environment,actual.database,actual.schema,actual.port)
    assert c.get('/api/v1/health').status_code==503
    assert c.get('/api/v1/canonical/status').status_code==503
    assert send(c,payload()).status_code==503
    store.identity=actual
    with store.connection() as conn:assert conn.execute('SELECT count(*) AS n FROM entities').fetchone()['n']==0


def test_real_pg_write_rollback_preserves_atomicity(postgres,monkeypatch):
    c,app,store,reads,user=postgres
    original=store._observe
    from bigbase.canonical_store import CanonicalError
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise CanonicalError('synthetic injected failure')
    monkeypatch.setattr(store,'_observe',fail)
    assert send(c,payload()).status_code==422
    with store.connection() as conn:
        for table in ['entities','identity_keys','observations','operations','outbox']:
            assert conn.execute('SELECT count(*) AS n FROM '+table).fetchone()['n']==0


def test_real_scalar_patch_preserves_previous_value_and_history(postgres):
    c,app,store,reads,user=postgres
    body=payload();created=send(c,body).json();row=store.page_history(created['id'])['items'][0]
    patch={'source_id':'manual','expected_version':1,'field_path':row['field_path'],
           'value':'SYNTHETIC UPDATED','observed_at':'2026-02-01T00:00:00Z'}
    url=f"/api/v1/canonical/people/{created['id']}/items/{row['item_id']}/value"
    response=c.patch(url,json=patch,headers={'Idempotency-Key':uuid4().hex})
    assert response.status_code==200,response.text
    assert response.json()['environment']=='staging'
    history=store.page_history(created['id'])['items']
    assert [r['input_value'] for r in history]==['SYNTHETIC RELEASE TEST','SYNTHETIC UPDATED']


def test_repeatable_read_concurrent_idempotency_has_one_commit(postgres):
    from concurrent.futures import ThreadPoolExecutor
    from bigbase.canonical_enrichment import prepare_enrichment
    c,app,store,reads,user=postgres
    body=payload();prepared=prepare_enrichment(body,'person');request_key=uuid4().hex
    def write(_):
        try:return store.apply_enrichment(prepared,actor_id=user['id'],request_key=request_key,expected_version=0)
        except psycopg.errors.SerializationFailure:return 'retry'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,range(2)))
    final=[write(None) if value=='retry' else value for value in results]
    assert all(isinstance(value,dict) for value in final)
    assert sorted(value['replayed'] for value in final)==[False,True]
    with store.connection() as conn:
        for table in ('entities','operations','observations','outbox'):
            assert conn.execute('SELECT count(*) AS n FROM '+table).fetchone()['n']==1
