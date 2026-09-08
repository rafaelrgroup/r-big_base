"""Synthetic contracts for versioned administrative fields and enrichment."""
import copy

import pyotp
import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app


@pytest.fixture
def catalog_env(tmp_path):
    app = create_app(tmp_path, testing=True)
    with app.state.store.transaction() as tx:
        user = app.state.security.create_user(tx, 'catalog-admin', 'synthetic-password-123', 'admin')
        secret = app.state.security.secret(user)
    with TestClient(app) as client:
        challenge = client.post('/api/v1/auth/login', json={
            'username': 'catalog-admin', 'password': 'synthetic-password-123',
        }).json()['challenge']
        authenticated = client.post('/api/v1/auth/otp', json={
            'challenge': challenge, 'code': pyotp.TOTP(secret).now(),
        })
        assert authenticated.status_code == 200
        client.headers['X-CSRF-Token'] = authenticated.json()['csrf']
        yield client, app


def definition(client, kind='text', **extra):
    response = client.post('/api/v1/admin/fields', json={
        'id': 'synthetic-field', 'name': 'Campo sintético', 'type': kind, **extra,
    })
    assert response.status_code == 200, response.text
    return response.json()


def enrich(client, value, *, field_id='synthetic-field', key='enrichment', **extra):
    entity_type = extra.get('entity_type', 'person')
    collection = 'people' if entity_type == 'person' else 'companies'
    return client.post('/api/v1/' + collection + '/enrich', json={
        'source_id': 'manual', 'observed_at': '2026-01-01T00:00:00Z',
        'items': [{'kind': 'custom', 'value': {'field_id': field_id, 'value': value}}],
        **extra,
    }, headers={'Idempotency-Key': key})


def test_versioned_definition_concurrency_and_immutable_history(catalog_env):
    client, app = catalog_env
    original = definition(client, 'enum', options=['Alfa', 'Beta'], multiple=False, scope='person')
    assert original['version'] == 1 and original['search_state'] == 'pending'
    assert original['definition_history'][0]['name'] == 'Campo sintético'
    url = '/api/v1/admin/fields/' + original['id']
    assert client.patch(url, json={'name': 'Sem versão'}).status_code == 428
    updated = client.patch(url, json={'name': 'Rótulo revisado', 'active': False}, headers={'If-Match': '"1"'})
    assert updated.status_code == 200, updated.text
    result = updated.json()
    assert result['version'] == 2 and result['active'] is False
    assert result['definition_history'][0] == original['definition_history'][0]
    assert [row['version'] for row in result['definition_history']] == [1, 2]
    assert client.patch(url, json={'active': True}, headers={'If-Match': '1'}).status_code == 409
    with app.state.store.transaction() as tx:
        old = app.state.store.get(tx, 'field_definition_version', original['id'] + ':1')
        assert old['definition'] == original['definition_history'][0]
        assert len(app.state.store.all(tx, 'field_definition_version')) == 2


@pytest.mark.parametrize('change', [
    {'type': 'integer'}, {'multiple': False}, {'scope': 'company'}, {'options': ['New']}, {'id': 'other-id'},
])
def test_incompatible_definition_changes_require_explicit_migration(catalog_env, change):
    client, _ = catalog_env
    original = definition(client)
    response = client.patch('/api/v1/admin/fields/' + original['id'], json=change, headers={'If-Match': '1'})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'field_definition_migration_required'
    assert client.get('/api/v1/admin/fields').json()['items'] == [original]


@pytest.mark.parametrize('extra', [
    {'multiple': 'false'}, {'multiple': 0}, {'multiple': None}, {'scope': 'organization'},
    {'type': 'enum', 'options': []}, {'type': 'enum', 'options': ['A', 'A']},
    {'type': 'enum', 'options': [False]}, {'options': ['Only enum']}, {'name': '  '},
    {'id': '../../unsafe'}, {'active': False}, {'sql': 'select 1'},
])
def test_definition_input_is_strict(catalog_env, extra):
    client, _ = catalog_env
    response = client.post('/api/v1/admin/fields', json={'name': 'Sintético', 'type': 'text', **extra})
    assert response.status_code == 422, response.text
    assert client.get('/api/v1/admin/fields').json()['items'] == []


@pytest.mark.parametrize(('kind', 'value', 'invalid', 'extra'), [
    ('text', '000123', 123, {}),
    ('integer', 0, False, {}),
    ('integer', 12, 12.0, {}),
    ('decimal', '0.000000000000000000000001', True, {}),
    ('decimal', 0.0, 'NaN', {}),
    ('boolean', False, 0, {}),
    ('date', '2024-02-29', '2025-02-29', {}),
    ('enum', 'Alfa', 'alfa', {'options': ['Alfa', 'Beta']}),
    ('url', 'https://example.invalid/synthetic?value=0', 'javascript:alert(1)', {}),
    ('url', 'http://example.invalid/', 'https://user:password@example.invalid/', {}),
])
def test_typed_values_preserve_original_type_and_reject_incompatible(catalog_env, kind, value, invalid, extra):
    client, _ = catalog_env
    definition(client, kind, **extra)
    response = enrich(client, value)
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert item['value']['value'] == value and type(item['value']['value']) is type(value)
    assert item['field_definition_version'] == 1 and item['classification_state'] == 'defined'
    assert item['fields']['value.value']['field_definition_version'] == 1
    assert all(row['field_definition_version'] == 1 for row in response.json()['observations'])
    rejected = enrich(client, invalid, key='incompatible')
    assert rejected.status_code == 422, rejected.text
    unknown = enrich(client, None, key='explicit-null')
    assert unknown.status_code == 200 and unknown.json()['items'][0]['value']['value'] is None


def test_reference_requires_existing_matching_entity_and_preserves_reference(catalog_env):
    client, _ = catalog_env
    definition(client, 'reference')
    target = client.post('/api/v1/companies/enrich', json={
        'entity_type': 'company', 'source_id': 'manual',
        'items': [{'kind': 'identity', 'value': {'name': 'Empresa Sintética'}}],
    }, headers={'Idempotency-Key': 'reference-target'}).json()
    value = {'entity_type': 'company', 'id': target['id']}
    accepted = enrich(client, value)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['items'][0]['value']['value'] == value
    for index, invalid in enumerate([
        {'entity_type': 'person', 'id': target['id']},
        {'entity_type': [], 'id': target['id']},
        {'entity_type': 'company', 'id': 'not-an-entity'}, target['id'],
    ]):
        assert enrich(client, invalid, key='invalid-reference-' + str(index)).status_code == 422


def test_unknown_fields_preserve_structured_false_zero_null_and_pending_warning(catalog_env):
    client, _ = catalog_env
    value = {'nested': [False, 0, None, [], {}], 'original': '000123'}
    response = enrich(client, value, field_id='unclassified-origin-field')
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert item['value']['value'] == value
    assert item['classification_state'] == 'pending' and item['field_definition_version'] is None
    assert 'preservado' in item['notes'][0]
    observation = next(row for row in response.json()['observations'] if row['path'] == 'value.value')
    assert observation['input_value'] == value and observation['classification_state'] == 'pending'
    # A later definition cannot rewrite an earlier value or claim it was validated.
    definition(client, 'boolean', id='unclassified-origin-field')
    detail = client.get('/api/v1/people/' + response.json()['id']).json()
    assert detail['items'][0] == item
    assert detail['observations'] == response.json()['observations']


def test_single_value_field_keeps_alternatives_and_flags_bound_to_each_value(catalog_env):
    client, _ = catalog_env
    definition(client, 'boolean', multiple=False)
    first = enrich(client, False, items=[{
        'kind': 'custom', 'value': {'field_id': 'synthetic-field', 'value': False}, 'flags': {'valid': True},
    }]).json()
    second = enrich(client, True, entity_id=first['id'], key='alternative').json()
    third = enrich(client, None, entity_id=first['id'], key='null-alternative').json()
    assert len(third['items']) == 3
    assert third['observations'][:len(first['observations'])] == first['observations']
    assert [item['value']['value'] for item in third['items']] == [False, True, None]
    assert third['items'][0]['flags']['valid'] is True
    assert third['items'][1]['flags'] == {} and third['items'][2]['flags'] == {}
    assert all(item['value_policy'] == 'scalar_per_item_alternatives_preserved' for item in third['items'])
    assert len(second['items']) == 2


def test_definition_scope_inactive_preservation_and_flag_validation(catalog_env):
    client, _ = catalog_env
    definition(client, scope='person')
    assert enrich(client, 'wrong scope', entity_type='company').status_code == 422
    entity = enrich(client, 'Preservado', key='person').json()
    item = entity['items'][0]
    response = client.patch('/api/v1/admin/fields/synthetic-field', json={'active': False}, headers={'If-Match': '1'})
    assert response.status_code == 200
    assert enrich(client, 'Novo', key='inactive', entity_id=entity['id']).status_code == 422
    assert client.get('/api/v1/people/' + entity['id']).json()['items'] == entity['items']
    validated = client.patch('/api/v1/people/' + entity['id'] + '/items/' + item['id'], json={
        'source_id': 'manual', 'observed_at': '2026-02-01T00:00:00Z', 'flags': {'valid': False},
    }, headers={'If-Match': str(item['version'])})
    assert validated.status_code == 200, validated.text
    assert validated.json()['observations'][-1]['field_definition_version'] == 1
    assert validated.json()['items'][0]['flags']['valid'] is False


def test_new_observation_binds_new_definition_without_rewriting_old_versions(catalog_env):
    client, _ = catalog_env
    definition(client)
    first = enrich(client, 'Valor original').json()
    client.patch('/api/v1/admin/fields/synthetic-field', json={'name': 'Rótulo atual'}, headers={'If-Match': '1'})
    updated = enrich(client, 'Valor original', key='updated-definition', entity_id=first['id'], observed_at='2026-02-01T00:00:00Z').json()
    assert len(updated['items']) == 1
    assert updated['items'][0]['field_definition_version'] == 2
    assert updated['observations'][:len(first['observations'])] == first['observations']
    assert all(row['field_definition_version'] == 2 for row in updated['observations'][len(first['observations']):])


def test_incompatible_custom_value_rejects_whole_enrichment_atomically(catalog_env):
    client, app = catalog_env
    definition(client, 'integer')
    entity = enrich(client, 0).json()
    with app.state.store.transaction() as tx:
        audit_before = tx.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    items = [{'kind': 'phone', 'value': {'number': '+5511998765432'}},
             {'kind': 'custom', 'value': {'field_id': 'synthetic-field', 'value': False}}]
    failed = enrich(client, 0, entity_id=entity['id'], key='atomic-retry', items=items)
    assert failed.status_code == 422, failed.text
    with app.state.store.transaction() as tx:
        assert app.state.store.get(tx, 'entity', entity['id']) == entity
        assert tx.execute('SELECT COUNT(*) FROM events').fetchone()[0] == audit_before
        assert tx.execute('SELECT COUNT(*) FROM idempotency WHERE key=?', ('atomic-retry',)).fetchone()[0] == 0
    corrected = copy.deepcopy(items)
    corrected[1]['value']['value'] = 1
    accepted = enrich(client, 0, entity_id=entity['id'], key='atomic-retry', items=corrected)
    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()['items']) == 3


def test_legacy_definition_defaults_do_not_mutate_persisted_record(catalog_env):
    client, app = catalog_env
    legacy = {'id': 'legacy', 'name': 'Definição antiga', 'type': 'text', 'created_at': '2026-01-01T00:00:00Z'}
    with app.state.store.transaction() as tx:
        app.state.store.put(tx, 'field', legacy)
    shown = client.get('/api/v1/admin/fields').json()['items'][0]
    assert shown['scope'] == 'both' and shown['options'] == [] and shown['multiple'] is True
    assert shown['version'] == 1 and shown['active'] is True and shown['search_state'] == 'pending'
    with app.state.store.transaction() as tx:
        assert app.state.store.get(tx, 'field', 'legacy') == legacy
    updated = client.patch('/api/v1/admin/fields/legacy', json={'name': 'Novo rótulo'}, headers={'If-Match': '1'})
    assert updated.status_code == 200, updated.text
    assert updated.json()['definition_history'][0]['name'] == legacy['name']


def test_patch_state_is_strict_and_multiple_means_repeated_items(catalog_env):
    client, _ = catalog_env
    original = definition(client, 'boolean', multiple=True)
    url = '/api/v1/admin/fields/synthetic-field'
    for invalid in ('false', 0, None):
        assert client.patch(url, json={'active': invalid}, headers={'If-Match': '1'}).status_code == 422
    assert client.get('/api/v1/admin/fields').json()['items'] == [original]
    assert enrich(client, [True, False], key='array-is-not-item').status_code == 422
    result = enrich(client, None, key='distinct-items', items=[
        {'kind': 'custom', 'value': {'field_id': 'synthetic-field', 'value': False}},
        {'kind': 'custom', 'value': {'field_id': 'synthetic-field', 'value': True}},
    ])
    assert result.status_code == 200, result.text
    assert [item['value']['value'] for item in result.json()['items']] == [False, True]


def test_regular_user_can_read_definitions_but_cannot_edit(catalog_env):
    client, app = catalog_env
    definition(client)
    with app.state.store.transaction() as tx:
        user = app.state.security.create_user(tx, 'catalog-reader', 'synthetic-password-456', 'user', ['read'])
        secret = app.state.security.secret(user)
    challenge = client.post('/api/v1/auth/login', json={'username': 'catalog-reader', 'password': 'synthetic-password-456'}).json()['challenge']
    completed = client.post('/api/v1/auth/otp', json={'challenge': challenge, 'code': pyotp.TOTP(secret).now()})
    client.headers['X-CSRF-Token'] = completed.json()['csrf']
    assert client.get('/api/v1/admin/fields').status_code == 200
    assert client.post('/api/v1/admin/fields', json={'name': 'Forbidden', 'type': 'text'}).status_code == 403
    assert client.patch('/api/v1/admin/fields/synthetic-field', json={'active': False}, headers={'If-Match': '1'}).status_code == 403
