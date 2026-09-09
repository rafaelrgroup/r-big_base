"""Writes through authenticated HTTP to run-owned PostgreSQL synthetic schemas."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.canonical_enrichment import prepare_enrichment
from bigbase.canonical_store import CanonicalError, VersionConflict, decode, json_text
from test_canonical_http import env, key_for, endpoint
from test_canonical_store import store


@pytest.fixture
def writable(env):
    client, app, reads, user = env
    reads.writes_enabled = True
    with app.state.store.transaction() as c:
        current = app.state.store.get(c, 'user', user['id'])
        current['permissions'] = ['read', 'enrich', 'validate']
        app.state.store.put(c, 'user', current)
    return env


def payload(**changes):
    return {'source_id': 'manual', 'source_record_id': uuid4().hex, 'expected_version': 0,
            'items': [{'kind': 'identity', 'key': 'identity', 'fields': [{'path': 'name', 'value': 'Pessoa fictícia'}]}], **changes}


def send(client, body, key=None, collection='people', **kwargs):
    return client.post('/api/v1/canonical/'+collection+'/enrich', content=json_text(body),
                       headers={'Content-Type': 'application/json', 'Idempotency-Key': key or uuid4().hex, **kwargs.pop('headers', {})}, **kwargs)


def counts(store):
    with store.connection() as c:
        return {name: c.execute('SELECT count(*) AS n FROM '+name).fetchone()['n']
                for name in ('entities', 'items', 'operations', 'observations', 'identity_keys', 'outbox', 'migration_jobs')}


@pytest.mark.parametrize('collection', ['people', 'companies'])
def test_create_multiple_items_exact_values_provenance_replay_and_local_separation(writable, store, collection):
    client, app, _, user = writable
    original = counts(store)
    values = [None, False, 0, '', Decimal('12345678901234567890.12345678901234567890'), 'nul\x00surrogate\ud800', {}, []]
    body = payload(items=[{'kind': 'custom', 'key': 'values', 'fields': [{'path': 'v'+str(i), 'value': value} for i, value in enumerate(values)]},
                          {'kind': 'username', 'key': 'instagram-1', 'fields': [{'path': 'platform', 'value': 'instagram'}, {'path': 'username', 'value': 'synthetic.one'}]},
                          {'kind': 'username', 'key': 'instagram-2', 'fields': [{'path': 'platform', 'value': 'instagram'}, {'path': 'username', 'value': 'synthetic.two'}]}])
    key = uuid4().hex
    response = send(client, body, key, collection)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['record_version'] == 1 and result['observations_created'] == 12 and not result['replayed']
    current = counts(store)
    assert current['outbox'] == original['outbox']+1 and current['migration_jobs'] == original['migration_jobs']
    replay = send(client, body, key, collection)
    assert replay.status_code == 200 and replay.json() == {**result, 'replayed': True}
    assert counts(store) == current
    history = store.page_history(result['id'])['items']
    for value, row in zip(values, history):
        assert type(row['input_value']) is type(value) and row['input_value'] == value
    assert all(row['source_id'] == 'manual' and row['actor_id'] == user['id'] and row['observed_at'] is None for row in history)
    assert len(store.get_entity(result['id'])['items']) == 3
    assert client.get('/api/v1/'+collection+'/'+result['id']).status_code == 404
    assert response.headers['cache-control'] == 'no-store'
    with app.state.store.transaction() as c:
        assert not app.state.store.all(c, 'entity')


def test_recursive_values_empty_containers_and_paths_are_preserved(writable, store):
    client, _, _, _ = writable
    body = payload(items=[{'kind': 'custom', 'key': 'unknown', 'fields': [{'path': 'raw', 'value': {'x/y~': [False, None, {}, [], 0, {'z': ''}]}}]}])
    result = send(client, body)
    assert result.status_code == 200, result.text
    history = store.page_history(result.json()['id'])['items']
    assert [row['input_value'] for row in history] == [False, None, {}, [], 0, '']
    assert history[0]['source_path'] == '/items/0/fields/0/value/x~1y~0/0'
    with store.connection() as c:
        rows = c.execute('SELECT container_type,length FROM source_containers WHERE operation_id=%s', (result.json()['operation_id'],)).fetchall()
    assert sorted((r['container_type'], r['length']) for r in rows) == [('array', 0), ('array', 6), ('object', 0), ('object', 1), ('object', 1)]


def test_version_conflict_replay_after_later_write_and_restart(writable, store, tmp_path):
    client, app, reads, _ = writable
    body = payload(); key = uuid4().hex
    result = send(client, body, key).json()
    second = deepcopy(body); second.update(entity_id=result['id'], expected_version=1)
    second['items'][0]['fields'][0]['value'] = 'Nome atualizado'
    assert send(client, second).json()['record_version'] == 2
    before = counts(store)
    assert send(client, second).json()['detail']['code'] == 'CANONICAL_VERSION_CONFLICT'
    assert counts(store) == before
    assert send(client, body, key).json() == {**result, 'replayed': True}
    assert send(client, second, key).status_code == 409
    restarted = create_app(tmp_path, testing=True, canonical_reads=reads)
    with TestClient(restarted) as retry:
        retry.cookies.update(client.cookies); retry.headers.update(client.headers)
        assert send(retry, body, key).json() == {**result, 'replayed': True}


def test_concurrent_versions_and_repeated_request_are_atomic(writable, store):
    _, _, _, user = writable
    body = payload(); prepared = prepare_enrichment(body, 'person')
    key = uuid4().hex
    def write(request_key):
        try:
            return store.apply_enrichment(prepared, actor_id=user['id'], request_key=request_key, expected_version=0)
        except VersionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(write, [key, key]))
    assert sorted(row['replayed'] for row in same) == [False, True]
    body = payload(); prepared = prepare_enrichment(body, 'person')
    with ThreadPoolExecutor(max_workers=2) as pool:
        different = list(pool.map(write, [uuid4().hex, uuid4().hex]))
    assert different.count('conflict') == 1


def test_atomic_rollback_on_failure_after_observations(writable, store, monkeypatch):
    client, _, _, _ = writable
    before = counts(store)
    real = store._observe
    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise CanonicalError('Synthetic failure after insert')
    monkeypatch.setattr(store, '_observe', fail)
    assert send(client, payload()).status_code == 422
    assert counts(store) == before


def test_late_future_undated_and_flags_stay_bound_to_the_correct_value(writable, store):
    client, _, _, _ = writable
    body = payload(items=[{'kind': 'phone', 'key': 'phone-1', 'fields': [{'path': 'number', 'value': 'synthetic-old', 'observed_at': '2026-01-01T00:00:00Z',
                        'flags': {'is_whatsapp': {'value': True, 'observed_at': '2026-01-01T00:00:00Z'}}}]}])
    result = send(client, body).json()
    update = deepcopy(body); update['expected_version'] = 1
    field = update['items'][0]['fields'][0]
    field.update(value='synthetic-new', observed_at='2026-02-01T00:00:00Z'); field.pop('flags')
    assert send(client, update).status_code == 200
    flag = next(row for row in store.page_fields(result['id'])['items'] if row['dimension'] == 'flag:is_whatsapp')
    assert not flag['applicable'] and flag['value'] is None
    late = deepcopy(body); late['expected_version'] = 2
    assert send(client, late).status_code == 200
    history = store.page_history(result['id'])['items']
    assert history[-1]['pending_reason'] == 'value_mismatch' and history[-2]['pending_reason'] == 'older_observation'
    update['expected_version'] = 3; field['value'] = 'future'; field['observed_at'] = '2099-01-01T00:00:00Z'
    assert send(client, update).status_code == 200
    update['expected_version'] = 4; field['observed_at'] = None
    assert send(client, update).status_code == 200
    history = store.page_history(result['id'])['items']
    assert [r['pending_reason'] for r in history[-2:]] == ['future_date', 'undated_against_dated']


def test_document_identity_normalization_conflict_and_collection(writable, store):
    client, _, _, _ = writable
    body = payload(document={'country': 'br', 'type': 'cpf', 'value': '529.982.247-25'})
    result = send(client, body)
    assert result.status_code == 200, result.text
    owner = result.json()['id']
    assert store.lookup_identity(country='BR', document_type='CPF', value='52998224725') == owner
    second = payload(document=body['document'])
    assert send(client, second).status_code == 409
    second['expected_version'] = store.get_entity(owner)['version']
    assert send(client, second).json()['id'] == owner
    wrong = deepcopy(body); wrong['document']['value'] = '111.444.777-35'; wrong['expected_version'] = 2
    before = counts(store)
    assert send(client, wrong).status_code == 409
    assert counts(store) == before
    assert send(client, body, collection='companies').status_code == 422
    assert send(client, payload(document={'type': 'CNPJ', 'country': 'BR', 'value': '11.222.333/0001-81'}), collection='companies').status_code == 200


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(actor_id='forged'), lambda b: b.update(expected_version=True),
    lambda b: b.update(expected_version=-1), lambda b: b.update(items=[]),
    lambda b: b['items'][0]['fields'][0].update(source_id='forged'),
    lambda b: b['items'][0]['fields'][0].update(flags={'valid': {'value': 0}}),
    lambda b: b['items'][0]['fields'][0].update(observed_at='2026-01-01'),
    lambda b: b['items'][0]['fields'].append(deepcopy(b['items'][0]['fields'][0])),
    lambda b: b['items'][0]['fields'][0].update(path='x/y'),
    lambda b: b['items'][0]['fields'][0].update(flags={'valid': {'value': True, 'expires_at': '2026-01-01T00:00:00Z'}}),
])
def test_invalid_contract_never_writes(writable, store, mutation):
    client, _, _, _ = writable
    body = payload(); mutation(body); before = counts(store)
    assert send(client, body).status_code == 422
    assert counts(store) == before


def test_authorization_sources_csrf_keys_revocation_and_disabled_destination(writable, store):
    client, app, reads, user = writable
    body = payload(); before = counts(store)
    read_key, _ = key_for(app, user, scopes=['read'])
    assert send(client, body, headers={'X-API-Key': read_key}).status_code == 403
    write_key, key = key_for(app, user, scopes=['enrich'], sources=['other'])
    assert send(client, body, headers={'X-API-Key': write_key}).status_code == 403
    body['source_id'] = 'missing'
    assert send(client, body).status_code == 422
    body['source_id'] = 'manual'
    token = client.headers.pop('X-CSRF-Token')
    assert send(client, body).status_code == 403
    client.headers['X-CSRF-Token'] = token
    reads.writes_enabled = False
    assert send(client, body).status_code == 503
    reads.writes_enabled = True
    assert counts(store) == before
    write_key, key = key_for(app, user, scopes=['enrich'], sources=['manual'])
    body['items'][0]['fields'][0]['flags'] = {'valid': {'value': False}}
    assert send(client, body, headers={'X-API-Key': write_key}).status_code == 403
    body['items'][0]['fields'][0].pop('flags')
    result = send(client, body, headers={'X-API-Key': write_key})
    assert result.status_code == 200
    assert store.page_history(result.json()['id'])['items'][0]['metadata']['api_key_id'] == key['public_id']
    with app.state.store.transaction() as c:
        key['active'] = False; app.state.store.put(c, 'api_key', key)
    assert send(client, body, headers={'X-API-Key': write_key}).status_code == 401


def test_wrong_expected_owner_and_identity_namespace_are_conflicts(writable, store):
    client, _, _, _ = writable
    body = payload(); result = send(client, body).json()
    other = payload(expected_version=1, entity_id=result['id'])
    before = counts(store)
    assert send(client, other).status_code == 409
    body['expected_version'] = 1
    assert send(client, body, collection='companies').status_code == 409
    assert counts(store) == before


def test_json_limits_duplicates_precision_and_destination_failure(writable, store, monkeypatch):
    client, _, reads, _ = writable
    before = counts(store)
    url = '/api/v1/canonical/people/enrich'
    for raw in ['{"source_id":"manual","source_id":"other"}', '{"value":NaN}', '{"value":1e99999999999999999999999999999999}']:
        assert client.post(url, content=raw, headers={'Content-Type': 'application/json'}).status_code == 422
    assert client.post(url, content='x'*(2*1024*1024+1)).status_code == 413
    def unavailable(): raise ValueError('private destination detail must not escape')
    monkeypatch.setattr(reads, 'verify', unavailable)
    result = send(client, payload())
    assert result.status_code == 503 and 'private destination' not in result.text
    assert counts(store) == before


def test_status_permissions_and_missing_idempotency_key(writable, store):
    client, app, _, user = writable
    status = client.get('/api/v1/canonical/status').json()
    assert status['writes_enabled'] and status['can_enrich'] and status['can_validate']
    body = payload(); before = counts(store)
    result = client.post('/api/v1/canonical/people/enrich', json=body)
    assert result.status_code == 422 and result.json()['detail']['code'] == 'IDEMPOTENCY_KEY_REQUIRED'
    with app.state.store.transaction() as c:
        current = app.state.store.get(c, 'user', user['id']); current['permissions'] = ['read']
        app.state.store.put(c, 'user', current)
    status = client.get('/api/v1/canonical/status').json()
    assert status['writes_enabled'] and not status['can_enrich'] and not status['can_validate']
    assert send(client, body).status_code == 403
    assert counts(store) == before


def test_explicit_confirmation_binding_metadata_and_independent_dates(writable, store):
    client, _, _, _ = writable
    body = payload(items=[{'kind': 'custom', 'key': 'numeric', 'fields': [{'path': 'value', 'value': 0,
        'observed_at': '2026-01-01T00:00:00Z', 'flags': {
            'valid': {'value': True, 'confirmed_value': False, 'observed_at': '2026-02-01T00:00:00Z'},
            'ownership_confirmed': {'value': False, 'checked_at': '2026-03-01T00:00:00Z',
                'expires_at': '2026-04-01T00:00:00Z', 'method': 'synthetic', 'reference': 'test-only'},
            'deliverable': {'value': None}}}]}])
    result = send(client, body)
    assert result.status_code == 200, result.text
    rows = {r['dimension']: r for r in store.page_history(result.json()['id'])['items']}
    assert rows['flag:valid']['pending_reason'] == 'value_mismatch'  # false is not zero
    assert rows['flag:ownership_confirmed']['observed_at'].startswith('2026-03-01')
    assert rows['flag:ownership_confirmed']['metadata']['method'] == 'synthetic'
    assert rows['flag:deliverable']['observed_at'] is None and rows['flag:deliverable']['normalized_value'] is None
    assert rows['value']['observed_at'].startswith('2026-01-01')


@pytest.mark.parametrize('case', ['atoms', 'depth', 'duplicate_structure'])
def test_expansion_limits_and_repeated_structured_field_reject_atomically(writable, store, case):
    client, _, _, _ = writable
    body = payload()
    if case == 'atoms':
        body['items'][0]['fields'][0]['value'] = list(range(1001))
    elif case == 'depth':
        value = 1
        for _ in range(26): value = [value]
        body['items'][0]['fields'][0]['value'] = value
    else:
        body['items'][0]['fields'].append({'path': 'name', 'value': {'nested': 'preserve'}})
    before = counts(store)
    assert send(client, body).status_code == 422
    assert counts(store) == before
