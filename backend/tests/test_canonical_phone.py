"""Phone HTTP contract on synthetic PostgreSQL; no provider or personal data access."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from bigbase.canonical_enrichment import prepare_enrichment, CONTRACT_VERSION
from bigbase.canonical_phone import PHONE_CONTRACT
from bigbase.canonical_store import CanonicalError, VersionConflict
from bigbase.domain import NORMALIZER_VERSION, normalize
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store


def phone(number='+551188765432', key='phone-1', **context):
    return {'kind': 'phone', 'key': key, 'phone_normalization': {'contract': PHONE_CONTRACT},
            'fields': [{'path': 'number', 'value': number, 'observed_at': '2026-02-01T00:00:00Z'},
                       *[{'path': name, 'value': value} for name, value in context.items()]]}


@pytest.mark.parametrize(('components', 'expected', 'decision'), [
    ({'number': '+551188765432'}, '+5511988765432', 'historical_conversion'),
    ({'number': '88765432', 'country': 'BR', 'ddd': '11'}, '+5511988765432', 'historical_conversion'),
    ({'number': '+5511998765432'}, '+5511998765432', 'canonical'),
    ({'number': '+551133334444'}, '+551133334444', 'canonical'),
    ({'number': '+551178765432'}, '+551178765432', 'canonical'),
    ({'number': '+442079460123', 'country': 'GB'}, '+442079460123', 'canonical'),
    ({'number': '1188765432'}, '1188765432', 'review'),
    ({'number': '88765432'}, '88765432', 'review'),
    ({'number': '+551168765432'}, '+551168765432', 'review'),
    ({'number': '+551188765432', 'country': 'US'}, '+551188765432', 'review'),
    ({'number': '+551188765432', 'ddd': '21'}, '+551188765432', 'review'),
    ({'number': '+1800FLOWERS'}, '+1800FLOWERS', 'review'),
    ({'number': '+551133334444 ext. 007'}, '+551133334444', 'canonical'),
    ({'number': '+551133334444 ext. 007', 'extension': '008'}, '+551133334444 ext. 007', 'review'),
    ({'number': ''}, '', 'review'),
])
def test_explicit_contract_reuses_rules_and_preserves_all_components(components, expected, decision):
    body = payload(items=[phone(**components)]); untouched = deepcopy(body)
    result = prepare_enrichment(body, 'person'); fact = result['facts'][0]
    normalized, notes = normalize('phone', components)
    assert body == untouched
    assert result['adapter_version'] == PHONE_CONTRACT
    assert fact['normalized_value'] == expected and fact['input_value'] == components['number']
    assert fact['normalization'] == NORMALIZER_VERSION
    assert fact['phone_normalization'] == normalized['phone_normalization']
    assert fact['phone_normalization']['input_components'] == components
    assert fact['phone_normalization']['decision'] == decision
    assert fact['normalization_notes'] == notes
    assert fact['status'] == ('pending' if decision == 'review' else 'normalized')
    assert len(result['facts']) == len(components) and not result['containers']
    assert all(not row['flags'] for row in result['facts'])
    assert all(row['normalization'] == 'preserved-no-inference' and 'observed_at' not in row
               for row in result['facts'][1:])


@pytest.mark.parametrize('value', [None, False, 0, 1188765432, Decimal('1.2'), [], {}, {'nested': 'phone'}])
def test_nontext_number_rejected_only_when_opted_in(value):
    body = payload(items=[phone(value)])
    with pytest.raises(CanonicalError): prepare_enrichment(body, 'person')
    body['items'][0].pop('phone_normalization')
    prepared = prepare_enrichment(body, 'person')
    assert prepared['adapter_version'] == CONTRACT_VERSION
    assert all('phone_normalization' not in fact for fact in prepared['facts'])


@pytest.mark.parametrize('mutate', [
    lambda i: i.update(phone_normalization=None),
    lambda i: i.update(phone_normalization={'contract': 'future'}),
    lambda i: i.update(phone_normalization={'contract': PHONE_CONTRACT, 'valid': True}),
    lambda i: i.update(kind='document'),
    lambda i: i['fields'][0].update(path='other'),
    lambda i: i['fields'].append({'path': 'ddd', 'value': 11}),
    lambda i: i['fields'].append({'path': 'country', 'value': None}),
    lambda i: i['fields'].append({'path': 'extension', 'value': []}),
    lambda i: i['fields'][0].update(value='1'*2049),
])
def test_invalid_contract_rejected_atomically(writable, store, mutate):
    client, _, _, _ = writable
    item = phone(); mutate(item); before = counts(store)
    assert send(client, payload(items=[item])).status_code == 422
    assert counts(store) == before


@pytest.mark.parametrize('collection', ['people', 'companies'])
def test_http_multiple_phones_audit_exact_flags_and_identities(writable, store, collection):
    client, _, _, user = writable
    first = phone('88765432', country='br', ddd='11')
    first['fields'][0]['flags'] = {'is_whatsapp': {'value': True}, 'valid': {'value': None}}
    first['fields'][1]['observed_at'] = '2026-01-01T00:00:00Z'
    first['fields'][2]['source_updated_at'] = '2026-01-02T01:00:00+01:00'
    body = payload(items=[first, phone('+551133334444', key='fixed'), phone('88765432', key='review'),
        {'kind': 'email', 'key': 'email', 'fields': [{'path': 'email', 'value': 'synthetic@example.invalid'}]}])
    before = counts(store); key = uuid4().hex
    response = send(client, body, key, collection)
    assert response.status_code == 200, response.text
    result = response.json(); owner = result['id']
    assert result['observations_created'] == 8 and response.headers['cache-control'] == 'no-store'
    assert counts(store)['outbox'] == before['outbox']+1
    rows = store.page_history(owner)['items']
    number = rows[0]
    assert number['normalized_value'] == '+5511988765432' and number['input_value'] == '88765432'
    assert number['source_path'] == '/items/0/fields/0/value'
    assert number['actor_id'] == user['id'] and number['source_id'] == 'manual'
    assert number['metadata']['context_fields']['ddd']['source_updated_at'].endswith('+01:00')
    assert rows[1]['pending_reason'] == rows[2]['pending_reason'] == 'value_mismatch'
    assert rows[1]['normalized_value'] is True and rows[2]['normalized_value'] is None
    assert rows[3]['normalized_value'] == 'br' and rows[3]['observed_at'].startswith('2026-01-01')
    assert rows[4]['source_updated_at'] == '2026-01-02T00:00:00+00:00'
    assert len(store.get_entity(owner)['items']) == 4
    assert counts(store)['identity_keys'] == before['identity_keys']+1  # source only, never phone identity
    after = counts(store)
    assert send(client, body, key, collection).json() == {**result, 'replayed': True}
    assert counts(store) == after
    # Equal normalized contacts with another external identity never merge owners.
    other = deepcopy(body); other['source_record_id'] = uuid4().hex
    assert send(client, other, collection=collection).json()['id'] != owner


@pytest.mark.parametrize('flag_value', [True, False, None])
def test_explicit_binding_and_literal_reversal_preserve_history(writable, store, flag_value):
    client, _, _, _ = writable
    item = phone(); item['fields'][0]['flags'] = {'is_whatsapp': {'value': flag_value,
        'confirmed_value': '+5511988765432', 'checked_at': '2026-03-01T00:00:00Z'}}
    body = payload(items=[item]); owner = send(client, body).json()['id']
    current = store.page_fields(owner)['items']; flag = next(r for r in current if r['dimension']=='flag:is_whatsapp')
    assert flag['applicable'] is True and flag['value'] is flag_value
    number = next(r for r in current if r['dimension']=='value')
    # A directed literal correction can restore original input; it appends an event.
    url = f"/api/v1/canonical/people/{owner}/items/{number['item_id']}/value"
    patch = {'source_id': 'manual', 'expected_version': 1, 'field_path': 'number',
             'value': number['input_value'], 'observed_at': '2026-04-01T00:00:00Z', 'reason': 'Revisão sintética'}
    assert client.patch(url, json=patch, headers={'Idempotency-Key': uuid4().hex}).status_code == 200
    current = store.page_fields(owner)['items']; new_flag = next(r for r in current if r['dimension']=='flag:is_whatsapp')
    assert new_flag['observation_id'] == flag['observation_id'] and not new_flag['applicable']
    history = store.page_history(owner)['items']
    assert len(history) == 3 and history[0]['metadata']['phone_normalization']['input_components']['number'] == '+551188765432'
    assert history[-1]['normalized_value'] == '+551188765432'
    assert history[-1]['metadata']['normalization'] == 'preserved-no-inference'


def test_partial_context_late_future_pending_and_absent_dates_do_not_replace_resolved(writable, store):
    client, _, _, _ = writable
    body = payload(items=[phone('88765432', country='BR', ddd='11')])
    result = send(client, body).json(); owner = result['id']
    before = {r['field_path']: r for r in store.page_fields(owner)['items']}
    for version, number, date in [(1, '+551133334444', '2025-01-01T00:00:00Z'),
                                   (2, '+551133334444', '2099-01-01T00:00:00Z'),
                                   (3, '+551133334444', None),
                                   (4, '88765432', '2026-04-01T00:00:00Z')]:
        changed = deepcopy(body); changed['expected_version'] = version
        item = phone(number); item['fields'][0]['observed_at'] = date; changed['items'] = [item]
        response = send(client, changed)
        assert response.status_code == 200, response.text
    after = {r['field_path']: r for r in store.page_fields(owner)['items']}
    assert {name:r['observation_id'] for name,r in after.items()} == {name:r['observation_id'] for name,r in before.items()}
    assert after['number']['value'] == '+5511988765432'
    latest = store.page_history(owner)['items'][-1]
    assert latest['pending_reason'] == 'pending_against_resolved'
    assert latest['metadata']['phone_normalization']['input_components'] == {'number': '88765432'}
    assert latest['metadata']['phone_output']['canonical_number'] is None
    assert store.get_entity(owner)['version'] == 5


def test_permission_source_revocation_and_replay_checked_for_normalization(writable, store):
    client, app, _, user = writable
    body = payload(items=[phone()]); before = counts(store)
    token, key = key_for(app, user, scopes=['enrich'], sources=['other'])
    assert send(client, body, headers={'X-API-Key': token}).status_code == 403
    token, key = key_for(app, user, scopes=['enrich'], sources=['manual'])
    body['items'][0]['fields'][0]['flags'] = {'is_whatsapp': {'value': True}}
    assert send(client, body, headers={'X-API-Key': token}).status_code == 403
    assert counts(store) == before
    body['items'][0]['fields'][0].pop('flags'); request_key = uuid4().hex
    response = send(client, body, request_key, headers={'X-API-Key': token})
    assert response.status_code == 200
    rows = store.page_history(response.json()['id'])['items']
    assert len(rows) == 1 and rows[0]['metadata']['api_key_id'] == key['public_id']
    with app.state.store.transaction() as c:
        key['active'] = False; app.state.store.put(c, 'api_key', key)
    after = counts(store)
    assert send(client, body, request_key, headers={'X-API-Key': token}).status_code == 401
    assert counts(store) == after


def test_normalized_concurrency_rollback_and_replay(writable, store, monkeypatch):
    client, _, _, user = writable
    body = payload(items=[phone()]); prepared = prepare_enrichment(body, 'person')
    key = uuid4().hex
    def write(request_key):
        try:
            return store.apply_enrichment(prepared, actor_id=user['id'], request_key=request_key, expected_version=0)
        except VersionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [key, key]))
    assert sorted(r['replayed'] for r in results) == [False, True]
    body = payload(items=[phone()]); prepared = prepare_enrichment(body, 'person')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [uuid4().hex, uuid4().hex]))
    assert results.count('conflict') == 1
    real = store._observe
    def fail(*args, **kwargs):
        real(*args, **kwargs); raise CanonicalError('Synthetic rollback after normalized observation')
    monkeypatch.setattr(store, '_observe', fail)
    before = counts(store)
    assert send(client, payload(items=[phone()])).status_code == 422
    assert counts(store) == before
