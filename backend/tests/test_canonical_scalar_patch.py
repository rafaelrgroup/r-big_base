"""Directed scalar HTTP updates against private, real PostgreSQL synthetic schemas."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.canonical_store import CanonicalError, VersionConflict, decode, json_text
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store


def seed(client, store, value='old', collection='people', **changes):
    body = payload(items=[{'kind': 'custom', 'key': 'one', 'fields': [
        {'path': 'value', 'value': value, 'observed_at': '2026-01-01T00:00:00Z'},
        {'path': 'untouched', 'value': False}]}], **changes)
    response = send(client, body, collection=collection)
    assert response.status_code == 200, response.text
    owner = response.json()['id']; row = store.page_history(owner)['items'][0]
    patch = {'source_id': 'manual', 'expected_version': 1, 'field_path': row['field_path'],
             'value': 'new', 'observed_at': '2026-02-01T00:00:00Z'}
    url = f"/api/v1/canonical/{collection}/{owner}/items/{row['item_id']}/value"
    return owner, url, patch, body


def patch(client, url, body, key=None, **headers):
    return client.patch(url, content=json_text(body), headers={'Content-Type': 'application/json',
        'Idempotency-Key': key or uuid4().hex, **headers})


@pytest.mark.parametrize('collection', ['people', 'companies'])
@pytest.mark.parametrize('value', [None, False, 0, '', Decimal('12345678901234567890.12345678901234567890'), 'nul\x00surrogate\ud800'])
def test_exact_value_provenance_partial_update_and_durable_replay(writable, store, collection, value):
    client, app, reads, user = writable
    owner, url, body, _ = seed(client, store, collection=collection)
    with app.state.store.transaction() as c:
        app.state.store.put(c, 'source', {'id': 'scalar-test', 'name': 'Synthetic', 'active': True})
    body.update(value=value, source_id='scalar-test', reason='Correção sintética', source_updated_at='2026-03-01T01:00:00+01:00')
    before = counts(store); untouched = next(r for r in store.page_fields(owner)['items'] if r['field_path']=='untouched')
    key=uuid4().hex; result=patch(client,url,body,key)
    assert result.status_code==200, result.text
    assert result.json()['record_version']==2 and result.json()['observations_created']==1
    assert result.headers['cache-control']=='no-store'
    after=counts(store)
    assert after=={**before,'observations':before['observations']+1,'operations':before['operations']+1,'outbox':before['outbox']+1}
    row=store.page_history(owner)['items'][-1]
    assert row['previous_value']=='old' and row['source_path']=='/value'
    assert type(row['input_value']) is type(value) and row['input_value']==value
    assert row['normalized_json']==json_text(value)
    assert row['source_id']=='scalar-test' and row['actor_id']==user['id']
    assert row['metadata']['reason']=='Correção sintética' and row['metadata']['identity_registry_updated'] is False
    assert row['source_updated_at']=='2026-03-01T00:00:00+00:00'
    assert row['observed_at']=='2026-02-01T00:00:00+00:00'
    assert next(r for r in store.page_fields(owner)['items'] if r['field_path']=='untouched')['observation_id']==untouched['observation_id']
    assert patch(client,url,body,key).json()=={**result.json(),'replayed':True} and counts(store)==after
    assert patch(client,url,{**body,'value':'changed'},key).status_code==409
    assert patch(client,url,body).json()['detail']['code']=='CANONICAL_VERSION_CONFLICT'
    assert client.get('/api/v1/'+collection+'/'+owner).status_code==404


def test_recursive_leaf_only_and_flags_remain_on_old_value_with_stable_snapshot(writable, store):
    client, _, _, _=writable
    owner,url,body,_=seed(client,store,{'a/b~':[False,0,{},[]]})
    assert body['field_path']=='value/a~1b~0/0'
    first=store.page_history(owner)['items'][0]
    flags={'source_id':'manual','expected_version':1,'field_path':body['field_path'],
           'value_observation_id':first['observation_id'],'flags':{'valid':{'value':True,'observed_at':'2026-02-01T00:00:00Z'}}}
    assert client.patch(url.removesuffix('/value')+'/flags',json=flags,headers={'Idempotency-Key':uuid4().hex}).status_code==200
    old_cut=store.entity_metadata(owner)['fields']['cursor']
    before={r['field_path']+r['dimension']:r['observation_id'] for r in store.page_fields(owner)['items']}
    body.update(expected_version=2,value=0)
    assert patch(client,url,body).status_code==200
    rows=store.page_fields(owner)['items'];flag=next(r for r in rows if r['dimension']=='flag:valid')
    assert flag['value'] is None and flag['applicable'] is False
    assert next(r for r in rows if r['field_path']==body['field_path'] and r['dimension']=='value')['value']==0
    for row in rows:
        if row['field_path']!=body['field_path'] or row['dimension']!='value':
            assert row['observation_id']==before[row['field_path']+row['dimension']]
    assert next(r for r in store.page_fields(owner,cursor=old_cut)['items'] if r['dimension']=='flag:valid')['value'] is True
    assert len([r for r in store.page_history(owner)['items'] if r['dimension'].startswith('flag:')])==1


@pytest.mark.parametrize('date,reason', [('2025-01-01T00:00:00Z','older_observation'),(None,'undated_against_dated'),('2099-01-01T00:00:00Z','future_date')])
def test_late_undated_future_preserve_current_and_record_history(writable,store,date,reason):
    client,_,_,_=writable;owner,url,body,_=seed(client,store)
    body['observed_at']=date
    assert patch(client,url,body).status_code==200
    row=store.page_history(owner)['items'][-1]
    assert row['pending_reason']==reason and not row['applied'] and row['input_value']=='new'
    assert next(r for r in store.page_fields(owner)['items'] if r['field_path']=='value')['value']=='old'


@pytest.mark.parametrize('mutation', [lambda b:b.pop('value'),lambda b:b.update(value={}),lambda b:b.update(value=[]),
    lambda b:b.update(value={'x':1}),lambda b:b.update(expected_version=True),lambda b:b.update(expected_version=0),
    lambda b:b.update(expected_version=2**63),lambda b:b.update(actor_id='forged'),lambda b:b.update(source_record_id='new-identity'),
    lambda b:b.update(flags={'valid':True}),lambda b:b.update(field_path='missing'),lambda b:b.update(field_path=''),
    lambda b:b.update(reason=''),lambda b:b.update(reason=42),lambda b:b.update(observed_at='2026-01-01'),
    lambda b:b.update(source_updated_at='invalid')])
def test_invalid_contract_has_no_side_effects(writable,store,mutation):
    client,_,_,_=writable;_,url,body,_=seed(client,store);mutation(body);before=counts(store)
    assert patch(client,url,body).status_code==422 and counts(store)==before


@pytest.mark.parametrize('raw', ['{"value":0,"value":false}', '{"value":NaN}', '{"value":Infinity}'])
def test_invalid_json_rejected_by_ingress(writable,store,raw):
    client,_,_,_=writable;_,url,_,_=seed(client,store);before=counts(store)
    assert client.patch(url,content=raw,headers={'Content-Type':'application/json','Idempotency-Key':uuid4().hex}).status_code==422
    assert counts(store)==before


def test_targets_containers_collection_item_and_identity_registry(writable,store):
    client,_,_,_=writable
    for value in ({},[]):
        _,url,body,_=seed(client,store,value);before=counts(store)
        assert patch(client,url,body).status_code==422 and counts(store)==before
    document={'type':'CPF','country':'BR','value':'52998224725'}
    owner,url,body,_=seed(client,store,document=document)
    row=next(r for r in store.page_history(owner)['items'] if r['field_path']=='number')
    url=f"/api/v1/canonical/people/{owner}/items/{row['item_id']}/value"
    body.update(field_path='number',value='11144477735');before=counts(store)
    assert patch(client,url.replace('/people/','/companies/'),body).status_code==422
    assert patch(client,url.replace(row['item_id'],str(uuid4())),body).status_code==422
    assert counts(store)==before
    assert patch(client,url,body).status_code==200
    assert store.lookup_identity(document_type='CPF',country='BR',value=document['value'])==owner
    assert store.lookup_identity(document_type='CPF',country='BR',value=body['value']) is None
    assert counts(store)['identity_keys']==before['identity_keys']


def test_authorization_active_source_csrf_revoked_replay_and_unavailable(writable,store,monkeypatch):
    client,app,reads,user=writable;owner,url,body,_=seed(client,store);before=counts(store)
    for scopes,sources in [(['read'],['manual']),(['validate'],['manual']),(['enrich'],['other'])]:
        token,_=key_for(app,user,scopes=scopes,sources=sources)
        assert patch(client,url,body,**{'X-API-Key':token}).status_code==403
    csrf=client.headers.pop('X-CSRF-Token');assert patch(client,url,body).status_code==403;client.headers['X-CSRF-Token']=csrf
    assert client.patch(url,json=body).status_code==422
    reads.writes_enabled=False;assert patch(client,url,body).status_code==503;reads.writes_enabled=True
    assert patch(client,url,{**body,'source_id':'missing'}).status_code==422
    with app.state.store.transaction() as c:
        source=app.state.store.get(c,'source','manual');source['active']=False;app.state.store.put(c,'source',source)
    assert patch(client,url,body).status_code==422
    with app.state.store.transaction() as c:
        source['active']=True;app.state.store.put(c,'source',source)
    assert counts(store)==before
    token,key=key_for(app,user,scopes=['enrich'],sources=['manual']);request_key=uuid4().hex
    assert patch(client,url,body,request_key,**{'X-API-Key':token}).status_code==200
    assert store.page_history(owner)['items'][-1]['metadata']['api_key_id']==key['public_id']
    with app.state.store.transaction() as c:
        key['active']=False;app.state.store.put(c,'api_key',key)
    assert patch(client,url,body,request_key,**{'X-API-Key':token}).status_code==401
    def fail():raise ValueError('private destination detail')
    monkeypatch.setattr(reads,'verify',fail)
    response=patch(client,url,body)
    assert response.status_code==503 and 'private destination detail' not in response.text


def test_rollback_concurrency_and_restart_after_lost_response(writable,store,monkeypatch,tmp_path):
    client,app,reads,user=writable;owner,url,body,_=seed(client,store);before=counts(store)
    real=store._observe
    def fail(*args,**kwargs):real(*args,**kwargs);raise CanonicalError('Synthetic insert failure')
    monkeypatch.setattr(store,'_observe',fail)
    assert patch(client,url,body).status_code==422 and counts(store)==before
    monkeypatch.setattr(store,'_observe',real)
    request_key=uuid4().hex
    def write(key):
        try:return store.apply_scalar_patch(body,owner_id=owner,item_id=url.split('/')[-2],entity_type='person',actor_id=user['id'],request_key=key)
        except VersionConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,[request_key,request_key]))
    assert sorted(r['replayed'] for r in results)==[False,True]
    body['expected_version']=2
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,[uuid4().hex,uuid4().hex]))
    assert results.count('conflict')==1
    body['expected_version']=1
    with TestClient(create_app(tmp_path,testing=True,canonical_reads=reads)) as retry:
        retry.cookies.update(client.cookies);retry.headers.update(client.headers)
        result=patch(retry,url,body,request_key)
        assert result.status_code==200 and result.json()['replayed'] and result.json()['record_version']==2
