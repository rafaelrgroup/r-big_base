"""Dedicated flag HTTP writes with real synthetic PostgreSQL and local test auth."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.canonical_store import CanonicalError, VersionConflict, decode
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store


def seed(client, store, value='synthetic-old', collection='people'):
    body = payload(items=[{'kind': 'custom', 'key': 'one', 'fields': [
        {'path': 'value', 'value': value, 'observed_at': '2026-01-01T00:00:00Z'}]}])
    receipt = send(client, body, collection=collection).json()
    row = store.page_history(receipt['id'])['items'][0]
    patch = {'source_id': 'manual', 'expected_version': 1, 'field_path': row['field_path'],
             'value_observation_id': row['observation_id'], 'flags': {'valid': {'value': True}}}
    url = f"/api/v1/canonical/{collection}/{receipt['id']}/items/{row['item_id']}/flags"
    return receipt['id'], url, patch, body


def validate(client, url, body, key=None, **headers):
    return client.patch(url, json=body, headers={'Idempotency-Key': key or uuid4().hex, **headers})


@pytest.mark.parametrize('collection', ['people', 'companies'])
@pytest.mark.parametrize('value', [None, False, 0, Decimal('12345678901234567890.12345678901234567890')])
def test_flag_only_exact_binding_provenance_and_replay(writable, store, collection, value):
    client, app, reads, user = writable
    owner, url, patch, _ = seed(client, store, value, collection)
    patch['flags'] = {'valid': {'value': False, 'reason': 'Ensaio de invalidação',
        'source_updated_at': '2026-03-01T01:00:00+01:00', 'checked_at': '2026-03-01T00:00:00Z',
        'expires_at': '2026-04-01T00:00:00Z', 'method': 'synthetic', 'reference': 'test-only'},
        'is_whatsapp': {'value': None}, 'ownership_confirmed': {'value': True}}
    before = counts(store); key = uuid4().hex
    result = validate(client, url, patch, key)
    assert result.status_code == 200, result.text
    assert result.json()['record_version'] == 2 and result.json()['observations_created'] == 3
    assert result.headers['cache-control'] == 'no-store'
    after = counts(store)
    assert after == {**before, 'observations': before['observations']+3, 'operations': before['operations']+1, 'outbox': before['outbox']+1}
    history = store.page_history(owner)['items']
    assert len([r for r in history if r['dimension'] == 'value']) == 1
    for row in history[1:]:
        assert row['actor_id'] == user['id'] and row['source_id'] == 'manual'
        assert row['metadata']['value_observation_id'] == patch['value_observation_id']
        bound = decode(row['metadata']['confirmed_value_json'])
        assert type(bound) is type(value) and bound == value
    fields = {r['dimension']: r for r in store.page_fields(owner)['items']}
    assert fields['flag:valid']['value'] is False and fields['flag:valid']['stale'] is True
    assert fields['flag:valid']['metadata']['reason'] == 'Ensaio de invalidação'
    assert fields['flag:ownership_confirmed']['value'] is True and fields['flag:ownership_confirmed']['stale'] is False
    assert fields['flag:is_whatsapp']['value'] is None and fields['flag:is_whatsapp']['observed_at'] is None
    assert validate(client, url, patch, key).json() == {**result.json(), 'replayed': True}
    assert counts(store) == after
    changed = deepcopy(patch); changed['flags']['is_whatsapp']['value'] = False
    assert validate(client, url, changed, key).status_code == 409
    assert validate(client, url, patch).json()['detail']['code'] == 'CANONICAL_VERSION_CONFLICT'


def test_old_value_is_preserved_without_confirming_new_value_and_snapshot_is_stable(writable, store):
    client, _, _, _ = writable
    owner, url, patch, enrichment = seed(client, store, False)
    patch['flags'] = {'valid': {'value': True, 'observed_at': '2026-02-01T00:00:00Z'}}
    assert validate(client, url, patch).status_code == 200
    old_cut = store.entity_metadata(owner)['fields']['cursor']
    enrichment['expected_version'] = 2
    enrichment['items'][0]['fields'][0].update(value=0, observed_at='2026-03-01T00:00:00Z')
    assert send(client, enrichment).status_code == 200
    patch['expected_version'] = 3
    patch['flags']['valid']['observed_at'] = '2026-04-01T00:00:00Z'
    assert validate(client, url, patch).status_code == 200
    history = store.page_history(owner)['items']
    assert history[-1]['pending_reason'] == 'value_mismatch'
    assert len([r for r in history if r['dimension'] == 'value']) == 2
    current = {r['dimension']: r for r in store.page_fields(owner)['items']}
    assert current['flag:valid']['value'] is None and not current['flag:valid']['applicable']
    old = {r['dimension']: r for r in store.page_fields(owner, cursor=old_cut)['items']}
    assert old['flag:valid']['value'] is True and old['value']['value'] is False
    patch.update(expected_version=4, value_observation_id=history[-2]['observation_id'])
    assert validate(client, url, patch).status_code == 200
    assert next(r for r in store.page_fields(owner)['items'] if r['dimension']=='flag:valid')['value'] is True


@pytest.mark.parametrize('date,reason', [('2026-01-01T00:00:00Z','older_observation'), (None,'undated_against_dated'), ('2099-01-01T00:00:00Z','future_date')])
def test_late_undated_future_flags_do_not_refresh_other_dimensions(writable, store, date, reason):
    client, _, _, _ = writable
    owner, url, patch, _ = seed(client, store)
    patch['flags'] = {'valid': {'value': True, 'observed_at':'2026-03-01T00:00:00Z'}, 'deliverable': {'value':False}}
    assert validate(client,url,patch).status_code==200
    initial = {r['dimension']:r for r in store.page_fields(owner)['items']}
    patch.update(expected_version=2, flags={'valid': {'value': None, 'observed_at':date}})
    assert validate(client,url,patch).status_code==200
    assert store.page_history(owner)['items'][-1]['pending_reason']==reason
    final = {r['dimension']:r for r in store.page_fields(owner)['items']}
    for dimension in initial:
        assert initial[dimension]['observation_id']==final[dimension]['observation_id']


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(expected_version=True), lambda b: b.update(expected_version=0),
    lambda b: b.update(actor_id='forged'), lambda b: b.update(value_observation_id=str(uuid4())),
    lambda b: b.update(field_path='other'), lambda b: b.update(flags={}),
    lambda b: b.update(flags={'unknown': {'value':True}}),
    lambda b: b.update(flags={'valid': {'value':0}}),
    lambda b: b.update(flags={'valid': {'value':False}}),
    lambda b: b.update(flags={'valid': {'value':True,'observed_at':'2026-01-01'}}),
    lambda b: b.update(flags={'valid': {'value':True,'expires_at':'2026-01-01T00:00:00Z'}}),
    lambda b: b.update(flags={'valid': {'value':None,'checked_at':'2026-01-01T00:00:00Z','expires_at':'2026-02-01T00:00:00Z'}}),
    lambda b: b.update(flags={'valid': {'value':True,'source_id':'forged'}}),
    lambda b: b.update(flags={'valid': {'value':True,'confirmed_value':False}}),
])
def test_invalid_contract_atomic(writable, store, mutation):
    client, _, _, _ = writable
    owner, url, patch, _ = seed(client, store)
    mutation(patch); before=counts(store)
    assert validate(client,url,patch).status_code==422
    assert counts(store)==before


def test_binding_rejects_wrong_entity_item_collection_flag_or_container(writable, store):
    client, _, _, _ = writable
    owner,url,patch,_=seed(client,store)
    other,other_url,other_patch,_=seed(client,store)
    before=counts(store)
    assert validate(client,url,other_patch).status_code==422
    assert validate(client, url.replace('/people/','/companies/'), patch).status_code==422
    assert validate(client, url.replace(url.split('/')[-2],str(uuid4())), patch).status_code==422
    assert counts(store)==before
    assert validate(client,url,patch).status_code==200
    patch.update(expected_version=2,value_observation_id=store.page_history(owner)['items'][-1]['observation_id'])
    assert validate(client,url,patch).status_code==422
    _,url,patch,_=seed(client,store,{})
    before=counts(store)
    assert validate(client,url,patch).status_code==422 and counts(store)==before


def test_permissions_sources_csrf_disabled_revoked_replay_and_sanitization(writable, store, monkeypatch):
    client, app, reads, user=writable
    owner,url,patch,_=seed(client,store); before=counts(store)
    for scopes,sources in [(['read'],['manual']), (['enrich'],['manual']), (['validate'],['other'])]:
        token,_=key_for(app,user,scopes=scopes,sources=sources)
        assert validate(client,url,patch,**{'X-API-Key':token}).status_code==403
    csrf=client.headers.pop('X-CSRF-Token')
    assert validate(client,url,patch).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.patch(url,json=patch).status_code==422
    reads.writes_enabled=False
    assert validate(client,url,patch).status_code==503
    reads.writes_enabled=True
    missing={**patch,'source_id':'missing'}
    assert validate(client,url,missing).status_code==422
    assert counts(store)==before
    token,key=key_for(app,user,scopes=['validate'],sources=['manual']); request_key=uuid4().hex
    result=validate(client,url,patch,request_key,**{'X-API-Key':token})
    assert result.status_code==200, result.text
    assert store.page_history(owner)['items'][-1]['metadata']['api_key_id']==key['public_id']
    with app.state.store.transaction() as c:
        key['active']=False; app.state.store.put(c,'api_key',key)
    assert validate(client,url,patch,request_key,**{'X-API-Key':token}).status_code==401
    def unavailable(): raise ValueError('private destination detail')
    monkeypatch.setattr(reads,'verify',unavailable)
    response=validate(client,url,patch)
    assert response.status_code==503 and 'private destination detail' not in response.text


def test_rollback_and_concurrent_versions_replay(writable, store, monkeypatch, tmp_path):
    client,app,reads,user=writable
    owner,url,patch,_=seed(client,store); before=counts(store)
    real=store._observe
    def fail(*args,**kwargs):
        real(*args,**kwargs); raise CanonicalError('Synthetic failure after observation insert')
    monkeypatch.setattr(store,'_observe',fail)
    assert validate(client,url,patch).status_code==422 and counts(store)==before
    monkeypatch.setattr(store,'_observe',real)
    request_key=uuid4().hex
    def write(key):
        try:
            return store.apply_validation(patch,owner_id=owner,item_id=url.split('/')[-2],entity_type='person',actor_id=user['id'],request_key=key)
        except VersionConflict: return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(write,[request_key,request_key]))
    assert sorted(r['replayed'] for r in results)==[False,True]
    patch['expected_version']=2
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(write,[uuid4().hex,uuid4().hex]))
    assert results.count('conflict')==1
    patch['expected_version']=1
    restarted=create_app(tmp_path,testing=True,canonical_reads=reads)
    with TestClient(restarted) as retry:
        retry.cookies.update(client.cookies); retry.headers.update(client.headers)
        replay=validate(retry,url,patch,request_key)
        assert replay.status_code==200 and replay.json()['replayed'] and replay.json()['record_version']==2


def test_preserved_malformed_legacy_expiry_does_not_break_reads(store):
    from test_canonical_store import atom, record, job, apply
    work=job(store)
    owner=apply(store,work,[record(work['source_id'],facts=[atom(flags={'valid':{'value':True,'expires_at':'unknown-original-format'}})])])['entity_ids'][0]
    row=next(r for r in store.page_fields(owner)['items'] if r['dimension']=='flag:valid')
    assert row['value'] is True and row['stale'] is None
    assert row['metadata']['expires_at']=='unknown-original-format'
