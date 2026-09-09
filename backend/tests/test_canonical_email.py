"""Email enrichment against a private synthetic PostgreSQL, without provider access."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from bigbase.canonical_email import EMAIL_CONTRACT
from bigbase.canonical_enrichment import prepare_enrichment, CONTRACT_VERSION
from bigbase.canonical_phone import PHONE_CONTRACT
from bigbase.canonical_store import CanonicalError, VersionConflict
from bigbase.domain import NORMALIZER_VERSION, normalize
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store


def email(value='First.Last+Tag@EXAMPLE.INVALID', key='email-1'):
    return {'kind': 'email', 'key': key, 'email_normalization': {'contract': EMAIL_CONTRACT},
            'fields': [{'path': 'email', 'value': value, 'observed_at': '2026-02-01T00:00:00Z'}]}


@pytest.mark.parametrize(('value', 'expected', 'valid'), [
    ('First.Last+Tag@EXAMPLE.INVALID', 'First.Last+Tag@example.invalid', True),
    ('First.Last+Tag@example.invalid', 'First.Last+Tag@example.invalid', True),
    ('UPPER@EXAMPLE.INVALID', 'UPPER@example.invalid', True),
    ('Álvaro+Teste@EXEMPLO.INVALID', 'Álvaro+Teste@exemplo.invalid', True),
    ('Case@DOMÍNIO.INVALID', 'Case@domínio.invalid', True),
    ('Case@XN--TESTE.INVALID', 'Case@xn--teste.invalid', True),
    (' two@EXAMPLE.INVALID ', ' two@example.invalid ', False),
    ('one@two@EXAMPLE.INVALID', 'one@two@example.invalid', False),
    ('one@HOST', 'one@host', False),
    ('no-at-sign', 'no-at-sign', False),
    ('', '', False),
    ('@EXAMPLE.INVALID', '@example.invalid', False),
])
def test_explicit_rule_preserves_local_part_input_and_version(value, expected, valid):
    body = payload(items=[email(value)]); untouched = deepcopy(body)
    result = prepare_enrichment(body, 'person'); fact = result['facts'][0]
    normalized, notes = normalize('email', {'email': value})
    assert body == untouched and result['adapter_version'] == EMAIL_CONTRACT
    assert fact['input_value'] == value and fact['normalized_value'] == expected == normalized['email']
    assert fact['normalization'] == NORMALIZER_VERSION and fact['email_contract'] == EMAIL_CONTRACT
    assert fact['email_normalization']['syntax_valid'] is valid
    assert fact['status'] == ('normalized' if valid else 'pending')
    assert fact['normalization_notes'] == notes and not fact['flags']
    assert len(result['facts']) == 1 and not result['containers']
    if '@' in value:
        local, domain = value.rsplit('@', 1)
        assert fact['email_normalization']['input_local_part'] == local
        assert fact['email_normalization']['input_domain'] == domain
        assert fact['email_normalization']['output_domain'] == domain.lower()


@pytest.mark.parametrize('value', [None, False, 0, Decimal('1.2'), [], {}, {'email': 'nested@example.invalid'}])
def test_nontext_rejected_explicitly_and_literal_contract_unchanged(value):
    body = payload(items=[email(value)])
    with pytest.raises(CanonicalError): prepare_enrichment(body, 'person')
    body['items'][0].pop('email_normalization')
    result = prepare_enrichment(body, 'person')
    assert result['adapter_version'] == CONTRACT_VERSION
    assert all('email_normalization' not in f for f in result['facts'])
    assert result['facts'][0]['normalized_value'] == (value['email'] if isinstance(value, dict) and value else value)


@pytest.mark.parametrize('mutate', [
    lambda i: i.update(email_normalization=None),
    lambda i: i.update(email_normalization={'contract': 'future'}),
    lambda i: i.update(email_normalization={'contract': EMAIL_CONTRACT, 'valid': True}),
    lambda i: i.update(kind='phone'),
    lambda i: i['fields'][0].update(path='other'),
    lambda i: i['fields'][0].update(value=0),
    lambda i: i['fields'][0].update(value='a'*2049),
    lambda i: i.update(phone_normalization={'contract': PHONE_CONTRACT}),
])
def test_invalid_contract_rolls_back_all_items(writable, store, mutate):
    client, _, _, _ = writable
    item = email(); mutate(item); before = counts(store)
    assert send(client, payload(items=[email(key='valid-first'), item])).status_code == 422
    assert counts(store) == before


@pytest.mark.parametrize('collection', ['people', 'companies'])
def test_http_multiple_items_original_dates_flags_and_replay(writable, store, collection):
    client, _, _, user = writable
    first = email(); first['fields'][0].update(source_updated_at='2026-02-01T01:00:00+01:00',
        flags={'deliverable': {'value': True}, 'valid': {'value': None}})
    first['fields'].append({'path': 'usage', 'value': 'commercial', 'observed_at': '2026-01-01T00:00:00Z'})
    body = payload(items=[first, email('second@example.invalid', key='email-2'), email('incomplete', key='review')])
    before = counts(store); key = uuid4().hex
    response = send(client, body, key, collection); assert response.status_code == 200, response.text
    result = response.json(); owner = result['id']; rows = store.page_history(owner)['items']
    assert result['observations_created'] == 6 and response.headers['cache-control'] == 'no-store'
    assert counts(store)['outbox'] == before['outbox'] + 1
    assert rows[0]['input_value'] == 'First.Last+Tag@EXAMPLE.INVALID'
    assert rows[0]['normalized_value'] == 'First.Last+Tag@example.invalid'
    assert rows[0]['source_path'] == '/items/0/fields/0/value'
    assert rows[0]['source_updated_at'] == '2026-02-01T00:00:00+00:00'
    assert rows[0]['metadata']['email_input_dates']['source_updated_at'].endswith('+01:00')
    assert all(r['actor_id'] == user['id'] and r['source_id'] == 'manual' for r in rows)
    assert rows[1]['pending_reason'] == rows[2]['pending_reason'] == 'value_mismatch'
    assert rows[1]['normalized_value'] is True and rows[2]['normalized_value'] is None
    assert rows[3]['normalized_value'] == 'commercial' and rows[3]['observed_at'].startswith('2026-01-01')
    assert rows[-1]['status'] == 'pending' and rows[-1]['input_value'] == 'incomplete'
    assert len(store.get_entity(owner)['items']) == 3
    assert counts(store)['identity_keys'] == before['identity_keys']+1
    after = counts(store)
    assert send(client, body, key, collection).json() == {**result, 'replayed': True}
    assert counts(store) == after
    conflict = deepcopy(body); conflict['items'][0]['fields'][0]['value'] = 'changed@example.invalid'
    assert send(client, conflict, key, collection).status_code == 409 and counts(store) == after
    other = deepcopy(body); other['source_record_id'] = uuid4().hex
    assert send(client, other, collection=collection).json()['id'] != owner


@pytest.mark.parametrize('value', [True, False, None])
def test_exact_binding_and_literal_reversal_do_not_move_flags(writable, store, value):
    client, _, _, _ = writable
    item = email(); item['fields'][0]['flags'] = {'deliverable': {'value': value,
        'confirmed_value': 'First.Last+Tag@example.invalid', 'checked_at': '2026-03-01T00:00:00Z'}}
    owner = send(client, payload(items=[item])).json()['id']
    current = store.page_fields(owner)['items']; field = next(r for r in current if r['dimension'] == 'value')
    flag = next(r for r in current if r['dimension'] == 'flag:deliverable')
    assert flag['applicable'] and flag['value'] is value
    url = f"/api/v1/canonical/people/{owner}/items/{field['item_id']}/value"
    patch = {'source_id': 'manual', 'expected_version': 1, 'field_path': 'email', 'value': field['input_value'],
             'observed_at': '2026-04-01T00:00:00Z', 'reason': 'Revisão sintética'}
    assert client.patch(url, json=patch, headers={'Idempotency-Key': uuid4().hex}).status_code == 200
    current = store.page_fields(owner)['items']; after = next(r for r in current if r['dimension'] == 'flag:deliverable')
    assert after['observation_id'] == flag['observation_id'] and not after['applicable']
    history = store.page_history(owner)['items']
    assert len(history) == 3 and history[0]['metadata']['email_normalization']['input_domain'] == 'EXAMPLE.INVALID'
    assert history[-1]['normalized_value'] == 'First.Last+Tag@EXAMPLE.INVALID'
    assert history[-1]['metadata']['normalization'] == 'preserved-no-inference'


def test_late_future_absent_and_pending_do_not_overwrite_current(writable, store):
    client, _, _, _ = writable
    item = email(); item['fields'].append({'path': 'usage', 'value': 'work'})
    body = payload(items=[item]); owner = send(client, body).json()['id']
    before = {r['field_path']: r['observation_id'] for r in store.page_fields(owner)['items']}
    for version, value, date in [(1, 'late@EXAMPLE.INVALID', '2025-01-01T00:00:00Z'),
                                  (2, 'future@EXAMPLE.INVALID', '2099-01-01T00:00:00Z'),
                                  (3, 'unknown@EXAMPLE.INVALID', None),
                                  (4, 'incomplete@HOST', '2026-04-01T00:00:00Z')]:
        changed = deepcopy(body); changed['expected_version'] = version
        item = email(value); item['fields'][0]['observed_at'] = date; changed['items'] = [item]
        response = send(client, changed); assert response.status_code == 200, response.text
    after = {r['field_path']: r['observation_id'] for r in store.page_fields(owner)['items']}
    assert before == after
    assert store.page_history(owner)['items'][-1]['pending_reason'] == 'pending_against_resolved'
    assert store.get_entity(owner)['version'] == 5


def test_mixed_phone_email_contracts_preserve_independent_audit():
    phone = {'kind': 'phone', 'key': 'phone', 'phone_normalization': {'contract': PHONE_CONTRACT},
             'fields': [{'path': 'number', 'value': '+551188765432'}]}
    result = prepare_enrichment(payload(items=[phone, email()]), 'person')
    assert result['adapter_version'] == EMAIL_CONTRACT
    assert result['facts'][0]['phone_contract'] == PHONE_CONTRACT
    assert result['facts'][1]['email_contract'] == EMAIL_CONTRACT
    assert result['facts'][0]['normalized_value'] == '+5511988765432'


def test_permission_source_revocation_and_replay(writable, store):
    client, app, _, user = writable
    body = payload(items=[email()]); before = counts(store)
    token, _ = key_for(app, user, scopes=['enrich'], sources=['other'])
    assert send(client, body, headers={'X-API-Key': token}).status_code == 403
    token, key = key_for(app, user, scopes=['enrich'], sources=['manual'])
    body['items'][0]['fields'][0]['flags'] = {'deliverable': {'value': True}}
    assert send(client, body, headers={'X-API-Key': token}).status_code == 403
    assert counts(store) == before
    body['items'][0]['fields'][0].pop('flags'); request_key = uuid4().hex
    response = send(client, body, request_key, headers={'X-API-Key': token}); assert response.status_code == 200
    rows = store.page_history(response.json()['id'])['items']
    assert len(rows) == 1 and rows[0]['metadata']['api_key_id'] == key['public_id']
    with app.state.store.transaction() as c:
        key['active'] = False; app.state.store.put(c, 'api_key', key)
    after = counts(store)
    assert send(client, body, request_key, headers={'X-API-Key': token}).status_code == 401
    assert counts(store) == after


def test_concurrent_normalization_atomic_rollback_and_replay(writable, store, monkeypatch):
    client, _, _, user = writable
    prepared = prepare_enrichment(payload(items=[email()]), 'person'); key = uuid4().hex
    def write(request_key):
        try:
            return store.apply_enrichment(prepared, actor_id=user['id'], request_key=request_key, expected_version=0)
        except VersionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [key, key]))
    assert sorted(r['replayed'] for r in results) == [False, True]
    prepared = prepare_enrichment(payload(items=[email()]), 'person')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [uuid4().hex, uuid4().hex]))
    assert results.count('conflict') == 1
    real = store._observe
    def fail(*args, **kwargs):
        real(*args, **kwargs); raise CanonicalError('Synthetic rollback after email observation')
    monkeypatch.setattr(store, '_observe', fail); before = counts(store)
    assert send(client, payload(items=[email()])).status_code == 422
    assert counts(store) == before
