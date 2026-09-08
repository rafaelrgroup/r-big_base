"""Authenticated HTTP with real private PostgreSQL and synthetic observations."""
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pyotp
import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.canonical_http import CanonicalReads
from bigbase.canonical_store import CanonicalStore, decode, json_text
from bigbase.security import hashed
from bigbase.rate_limit import RateLimitUnavailable
from test_canonical_store import store, atom, record, job, apply
from test_canonical_search_reader import reader, CRITERIA


@pytest.fixture
def env(store, tmp_path):
    reads = CanonicalReads(store, expected_deployment_id=store.deployment_info()['deployment_id'])
    app = create_app(tmp_path, testing=True, canonical_reads=reads)
    with app.state.store.transaction() as c:
        user = app.state.security.create_user(c, 'canonical-user', 'synthetic-password-123', 'user')
        secret = app.state.security.secret(user)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'canonical-user', 'password': 'synthetic-password-123'}).json()
        assert client.get('/api/v1/canonical/status').status_code == 401
        response = client.post('/api/v1/auth/otp', json={'challenge': login['challenge'], 'code': pyotp.TOTP(secret).now()})
        assert response.status_code == 200
        client.headers['X-CSRF-Token'] = response.json()['csrf']
        yield client, app, reads, {'id':user['id']}


def seed(store, *, count=25, entity_type='person'):
    work = job(store)
    facts = [atom('/'+str(i), i, target='field'+str(i), kind='custom', key='item'+str(i)) for i in range(count)]
    result = apply(store, work, [record(work['source_id'], facts=facts, entity_type=entity_type, document=work['source_id'])])
    return result['entity_ids'][0], work


def endpoint(owner, kind='', collection='people'):
    return '/api/v1/canonical/'+collection+'/'+owner+('/'+kind if kind else '')


def post(client, owner, kind, **body):
    response = client.post(endpoint(owner, kind), json=body)
    assert response.status_code == 200, response.text
    return decode(response.text)


def key_for(app, user, *, scopes=None, sources=None):
    token = 'synthetic-'+uuid4().hex
    key = {'id': hashed(token), 'public_id': uuid4().hex, 'user_id': user['id'], 'active': True,
           'scopes': scopes or ['read'], 'sources': sources or ['manual'],
           'expires_at': (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}
    with app.state.store.transaction() as c:
        app.state.store.put(c, 'api_key', key)
    return token, key


def test_disabled_explicit_and_local_storage_is_not_fallback(tmp_path):
    app = create_app(tmp_path, testing=True)
    with TestClient(app) as client:
        assert client.get('/api/v1/canonical/status').status_code == 401
        assert client.get(endpoint(str(uuid4()))).status_code == 401
    assert not (tmp_path/'canonical.sqlite3').exists()


@pytest.mark.parametrize('dsn', ['dbname=bigbase_test', 'host=127.0.0.1 port=18769 dbname=bigbase_test',
    'host=/tmp port=5432 dbname=bigbase_test', 'host=/tmp port=18769 dbname=production',
    'host=/tmp hostaddr=127.0.0.1 port=18769 dbname=bigbase_test'])
def test_configuration_rejects_implicit_or_real_destination_before_connection(dsn, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('Forbidden configuration attempted a connection')
    monkeypatch.setattr(psycopg, 'connect', forbidden)
    with pytest.raises(ValueError):
        CanonicalReads(CanonicalStore(dsn), expected_deployment_id=str(uuid4()))


def test_lookup_metadata_company_namespace_and_no_sqlite_entity(env, store):
    client, app, reads, user = env
    owner, work = seed(store, entity_type='company')
    url = '/api/v1/canonical/companies/lookup'
    result = client.post(url, json={'source_id':work['source_id'], 'source_record_id':'1'})
    assert result.status_code == 200
    assert result.json()['id'] == owner and result.json()['entity_type'] == 'company'
    assert all(result.json()[kind]['cursor'].startswith('ch1_') for kind in ('items','fields','history'))
    by_doc = client.post(url, json={'country':'TEST', 'document_type':'SYNTHETIC','value':work['source_id']})
    assert by_doc.status_code == 200 and by_doc.json()['id'] == owner
    assert client.get(endpoint(owner)).status_code == 404
    assert client.get('/api/v1/companies/'+owner).status_code == 404
    assert client.post(url, json={'source_id': work['source_id']}).status_code == 422
    with app.state.store.transaction() as c:
        assert not app.state.store.all(c, 'entity')
    assert client.get('/api/v1/canonical/status').json()['auth_storage'] == 'sqlite-local-single-process'


def test_metadata_and_child_collections_keep_cut_across_writes(env, store):
    client, _, _, _ = env
    owner, work = seed(store)
    meta = client.get(endpoint(owner)).json()
    apply(store, work, [record(work['source_id'], facts=[atom('/new', 'late-added', target='new', key='new')], source_version='2')])
    page = post(client, owner, 'items', cursor=meta['items']['cursor'], limit=2)
    collected = []
    first_item = page['items'][0]
    while True:
        assert page['snapshot']['entity_version'] == meta['version']
        collected.extend(page['items'])
        if not page['has_more']: break
        page = post(client, owner, 'items', cursor=page['next_cursor'], limit=2)
    assert len(collected) == 25 and len({i['id'] for i in collected}) == 25
    for kind in ('fields','history'):
        child = post(client, owner, kind, item_id=first_item['id'], cursor=first_item[kind]['cursor'])
        assert len(child['items']) == 1 and child['snapshot']['entity_version'] == meta['version']
    entire = post(client, owner, 'history', cursor=meta['history']['cursor'], limit=200)
    assert len(entire['items']) == 25
    assert client.get(endpoint(owner)).json()['version'] == 2


def test_exact_http_numbers_nul_and_tristate_history(env, store):
    client, _, _, _ = env
    work = job(store)
    values = [None, False, 0, '', Decimal('12345678901234567890.12345678901234567890'), 'nul\x00surrogate\ud800']
    facts = [atom('/'+str(i), value, target='v'+str(i), key='same', kind='custom') for i,value in enumerate(values)]
    owner = apply(store, work, [record(work['source_id'], facts=facts)])['entity_ids'][0]
    result = post(client, owner, 'fields', limit=20)
    by_path = {row['field_path']:row['value'] for row in result['items']}
    for i,value in enumerate(values):
        assert type(by_path['v'+str(i)]) is type(value) and by_path['v'+str(i)] == value
    history = post(client, owner, 'history', limit=20)
    assert [row['input_value'] for row in history['items']] == values
    assert all(row['actor_id'] == 'test-actor' and row['source_updated_at'] is None for row in history['items'])


def test_cursor_binding_filters_owner_credential_and_permissions(env, store):
    client, app, reads, user = env
    owner, _ = seed(store)
    other, _ = seed(store)
    page = post(client, owner, 'items', limit=1)
    cursor = page['next_cursor']
    for url,body in [(endpoint(other,'items'),{'cursor':cursor}), (endpoint(owner,'fields'),{'cursor':cursor}),
                     (endpoint(owner,'items'),{'cursor':cursor,'order':'desc'}),
                     (endpoint(owner,'items'),{'cursor':cursor,'kind':'phone'})]:
        assert client.post(url,json=body).status_code == 422
    token, key = key_for(app, user)
    assert client.post(endpoint(owner,'items'),json={'cursor':cursor},headers={'X-API-Key':token}).status_code == 422
    with app.state.store.transaction() as c:
        current = app.state.store.get(c,'user',user['id']); current['permissions'].append('export'); app.state.store.put(c,'user',current)
    assert client.post(endpoint(owner,'items'),json={'cursor':cursor}).status_code == 422
    # A fresh read is permitted under freshly evaluated permissions.
    assert client.get(endpoint(owner)).status_code == 200


def test_api_key_scope_source_revocation_and_user_disable(env, store):
    client, app, _, user = env
    owner, work = seed(store)
    token, key = key_for(app, user)
    headers = {'X-API-Key':token}
    assert client.get(endpoint(owner), headers=headers).status_code == 200
    assert client.post('/api/v1/canonical/people/lookup', json={'source_id':work['source_id'],'source_record_id':'1'},headers=headers).status_code == 403
    first = client.post(endpoint(owner,'items'), json={'limit':1}, headers=headers).json()
    with app.state.store.transaction() as c:
        key['active'] = False; app.state.store.put(c,'api_key',key)
    assert client.post(endpoint(owner,'items'), json={'cursor':first['next_cursor']}, headers=headers).status_code == 401
    limited, _ = key_for(app, user, scopes=['enrich'])
    assert client.get(endpoint(owner), headers={'X-API-Key':limited}).status_code == 403
    with app.state.store.transaction() as c:
        current = app.state.store.get(c,'user',user['id']); current['active']=False;app.state.store.put(c,'user',current)
    assert client.get(endpoint(owner)).status_code == 401


def test_csrf_and_unknown_parameters_never_ignored(env, store):
    client, _, _, _ = env
    owner, _ = seed(store)
    assert client.post(endpoint(owner,'items'),json={'sort':'name'}).status_code == 422
    assert client.post(endpoint(owner,'items'),json={'authorization':{'can_search':True}}).status_code == 422
    for limit in [True,0,201,1.0,'2']:
        assert client.post(endpoint(owner,'items'),json={'limit':limit}).status_code == 422
    client.headers.pop('X-CSRF-Token')
    assert client.post(endpoint(owner,'items'),json={}).status_code == 403
    assert client.get(endpoint(owner)).status_code == 200


def test_expired_tampered_raw_cursor_and_restarted_http(env, store, tmp_path):
    client, app, reads, user = env
    owner, _ = seed(store)
    page = post(client,owner,'items',limit=1)
    cursor = page['next_cursor']
    raw = store.page_items(owner,limit=1)['next_cursor']
    for invalid in [raw,cursor[:-1]+'!', 'x'*5000]:
        assert client.post(endpoint(owner,'items'),json={'cursor':invalid}).status_code == 422
    # Recreate the application using the same isolated auth directory/key.
    # The persistent cipher is tested by a fresh app pointing at the same fixture path.
    another = create_app(tmp_path,testing=True,canonical_reads=reads)
    with TestClient(another) as restarted:
        restarted.cookies.update(client.cookies); restarted.headers.update(client.headers)
        assert restarted.post(endpoint(owner,'items'),json={'cursor':cursor}).status_code == 200
    with store.connection() as c:
        c.execute("UPDATE read_cursors SET started_at=clock_timestamp()-interval '2 hours', expires_at=clock_timestamp()-interval '1 hour', deadline_at=clock_timestamp()-interval '1 second' WHERE owner_id=%s",(owner,))
    assert client.post(endpoint(owner,'items'),json={'cursor':cursor}).status_code == 422


def test_destination_change_fails_closed_without_payload(env, store, monkeypatch):
    client, _, reads, _ = env
    owner, _ = seed(store)
    info = store.deployment_info(); info['environment'] = 'production'
    monkeypatch.setattr(store,'deployment_info',lambda: info)
    response = client.get(endpoint(owner))
    assert response.status_code == 503 and 'production' not in response.text


def test_read_uses_existing_limits_and_redis_failure(env, store, monkeypatch):
    client, app, _, _ = env
    owner, _ = seed(store)
    # Non-testing runtime, same isolated auth/PG stores, controlled limiter only.
    class Limit:
        def __init__(self): self.fail = False; self.deny = False; self.keys=[]
        def consume(self, key, **kwargs):
            self.keys.append(key)
            if self.fail: raise RateLimitUnavailable()
            return not (self.deny and key.startswith('user:'))
    limit = Limit()
    monkeypatch.setattr('bigbase.api.Limiter',lambda:limit)
    # tmp auth path supplied by sqlite connection filename, without reading auth rows.
    with app.state.store.transaction() as c:
        from pathlib import Path
        root = Path(c.execute('PRAGMA database_list').fetchone()[2]).parent
    other = create_app(root, canonical_reads=CanonicalReads(store,expected_deployment_id=store.deployment_info()['deployment_id']))
    with TestClient(other) as runtime:
        runtime.cookies.update(client.cookies)
        assert runtime.get(endpoint(owner)).status_code == 200
        assert any(k.startswith('user:') for k in limit.keys)
        limit.deny=True
        assert runtime.get(endpoint(owner)).status_code == 429
        limit.fail=True
        assert runtime.get(endpoint(owner)).status_code == 503


def test_search_http_postgres_hydration_versions_cursor_and_close(env, store):
    client, app, reads, user = env
    owner, work = seed(store)
    owner2, _ = seed(store)
    search_reader, fake, _ = reader()
    reads.search_reader = search_reader
    fake.docs = sorted([{'id':owner,'record_version':1},{'id':owner2,'record_version':1}], key=lambda row:row['id'])
    apply(store,work,[record(work['source_id'],facts=[atom('/change','Updated synthetic')],source_version='2')])
    url = '/api/v1/canonical/people/search'
    first = client.post(url,json={'filters':CRITERIA,'page_size':1})
    assert first.status_code == 200, first.text
    second = client.post(url,json={'filters':CRITERIA,'page_size':1,'cursor':first.json()['next_cursor']})
    assert second.status_code == 200, second.text
    hits = first.json()['items']+second.json()['items']
    changed = next(hit for hit in hits if hit['id']==owner)
    assert changed['indexed_version'] == 1 and changed['record_version'] == 2 and changed['indexing_pending'] is True
    assert all(hit['entity']['items']['cursor'].startswith('ch1_') for hit in hits)
    token, key = key_for(app,user)
    assert client.post(url,json={'filters':CRITERIA,'page_size':1,'cursor':first.json()['next_cursor']},headers={'X-API-Key':token}).status_code == 422
    assert client.post(url,json={'sort':[{'field':'name','direction':'asc'}]}).status_code == 422
    assert client.post(url,json={'filters':{'field':'arbitrary','value':'x'}}).status_code == 422
    assert client.post('/api/v1/canonical-search/close',json={'release_cursor':second.json()['release_cursor']}).json() == {'released':True}
    assert not fake.pits


def test_late_events_and_value_bound_confirmation_in_http(env, store):
    client, _, _, _ = env
    work = job(store)
    old = atom('/number', 'number-A', target='number', kind='phone', key='contact', observed_at='2025-01-01T00:00:00Z',
               flags={'is_whatsapp':{'value':True}})
    owner = apply(store,work,[record(work['source_id'],facts=[old])])['entity_ids'][0]
    meta = client.get(endpoint(owner)).json()
    current = atom('/number','number-B',target='number',kind='phone',key='contact',observed_at='2025-03-01T00:00:00Z')
    late = atom('/number','number-late',target='number',kind='phone',key='contact',observed_at='2025-02-01T00:00:00Z')
    apply(store,work,[record(work['source_id'],facts=[current])])
    apply(store,work,[record(work['source_id'],facts=[late])])
    initial = post(client,owner,'fields',cursor=meta['fields']['cursor'])['items']
    assert next(row for row in initial if row['dimension']=='flag:is_whatsapp')['value'] is True
    present = post(client,owner,'fields')['items']
    assert next(row for row in present if row['dimension']=='value')['value'] == 'number-B'
    flag = next(row for row in present if row['dimension']=='flag:is_whatsapp')
    assert flag['value'] is None and flag['applicable'] is False and flag['normalized_value'] is True
    history = post(client,owner,'history')['items']
    assert len(history)==4 and history[-1]['applied'] is False and history[-1]['input_value']=='number-late'


def test_cursor_is_rejected_for_another_human_session(env, store):
    client, app, _, _ = env
    owner, _ = seed(store)
    page = post(client,owner,'items',limit=1)
    with app.state.store.transaction() as c:
        user = app.state.security.create_user(c,'other-synthetic','synthetic-password-123','admin')
        secret = app.state.security.secret(user)
    with TestClient(app) as other:
        login = other.post('/api/v1/auth/login',json={'username':'other-synthetic','password':'synthetic-password-123'}).json()
        result = other.post('/api/v1/auth/otp',json={'challenge':login['challenge'],'code':pyotp.TOTP(secret).now()}).json()
        other.headers['X-CSRF-Token']=result['csrf']
        assert other.get(endpoint(owner)).status_code==200
        assert other.post(endpoint(owner,'items'),json={'cursor':page['next_cursor']}).status_code==422
