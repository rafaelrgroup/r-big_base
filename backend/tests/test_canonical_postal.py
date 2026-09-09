"""Postal enrichment against private synthetic PostgreSQL, without address lookup."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from bigbase.canonical_postal import POSTAL_CONTRACT
from bigbase.canonical_enrichment import prepare_enrichment, CONTRACT_VERSION
from bigbase.canonical_phone import PHONE_CONTRACT
from bigbase.canonical_email import EMAIL_CONTRACT
from bigbase.canonical_store import CanonicalError, VersionConflict
from bigbase.domain import NORMALIZER_VERSION, normalize
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store


def postal(value='00123-456', key='postal-1', country='BR'):
    fields = [{'path': 'postal_code', 'value': value, 'observed_at': '2026-02-01T00:00:00Z'}]
    if country is not None: fields.append({'path': 'country', 'value': country})
    return {'kind': 'address', 'key': key, 'postal_normalization': {'contract': POSTAL_CONTRACT}, 'fields': fields}


@pytest.mark.parametrize(('value', 'country', 'expected', 'valid'), [
    ('00123-456', 'BR', '00123456', True),
    ('00123456', 'BR', '00123456', True),
    (' 00 123-456 ', 'BR', '00123456', True),
    ('00000-000', 'BR', '00000000', True),
    ('00123-456', None, '00123-456', None),
    ('SW1A 1AA', 'GB', 'SW1A 1AA', None),
    ('H0H 0H0', 'CA', 'H0H 0H0', None),
    ('00123-4567', 'US', '00123-4567', None),
    ('00123-456', 'ZZ', '00123-456', None),
    ('123-456', 'BR', '123-456', False),
    ('01234/567', 'BR', '01234/567', False),
    ('１２３４５６７８', 'BR', '１２３４５６７８', False),
    ('0012A-456', 'BR', '0012A-456', False),
    ('', 'BR', '', False),
])
def test_explicit_country_rule_preserves_input_and_version(value, country, expected, valid):
    body = payload(items=[postal(value, country=country)]); untouched = deepcopy(body)
    result = prepare_enrichment(body, 'person'); fact = result['facts'][0]
    candidate, notes = normalize('address', {'postal_code': value, 'country': country})
    assert body == untouched and result['adapter_version'] == POSTAL_CONTRACT
    assert fact['input_value'] == value and fact['normalized_value'] == expected
    assert fact['normalization'] == NORMALIZER_VERSION and fact['postal_contract'] == POSTAL_CONTRACT
    assert fact['postal_normalization']['syntax_valid'] is valid
    assert fact['postal_normalization']['country'] == country
    assert fact['postal_normalization']['candidate_value'] == candidate['postal_code']
    assert fact['status'] == ('normalized' if valid else 'pending')
    assert fact['normalization_notes'] == notes and not fact['flags']
    assert len(result['facts']) == (1 if country is None else 2) and not result['containers']
    if country is not None:
        assert result['facts'][1]['normalized_value'] == country
        assert result['facts'][1]['normalization'] == 'preserved-no-inference'
        assert fact['context_fields'] == {'country': {'value': country}}
    else: assert fact['context_fields'] == {}


@pytest.mark.parametrize('value', [None, False, 0, Decimal('1.2'), [], {}, {'postal': 'nested-code'}])
def test_nontext_rejected_explicitly_and_literal_contract_unchanged(value):
    body = payload(items=[postal(value)])
    with pytest.raises(CanonicalError): prepare_enrichment(body, 'person')
    body['items'][0].pop('postal_normalization')
    result = prepare_enrichment(body, 'person')
    assert result['adapter_version'] == CONTRACT_VERSION
    assert all('postal_normalization' not in f for f in result['facts'])
    assert result['facts'][0]['normalized_value'] == (value['postal'] if isinstance(value, dict) and value else value)


@pytest.mark.parametrize('mutate', [
    lambda i: i.update(postal_normalization=None),
    lambda i: i.update(postal_normalization={'contract': 'future'}),
    lambda i: i.update(postal_normalization={'contract': POSTAL_CONTRACT, 'valid': True}),
    lambda i: i.update(kind='phone'),
    lambda i: i['fields'][0].update(path='other'),
    lambda i: i['fields'][0].update(value=0),
    lambda i: i['fields'][0].update(value='a'*2049),
    lambda i: i.update(phone_normalization={'contract': PHONE_CONTRACT}),
    lambda i: i['fields'][1].update(value=None),
    lambda i: i['fields'][1].update(value=False),
    lambda i: i['fields'][1].update(value=0),
    lambda i: i['fields'][1].update(value='br'),
    lambda i: i['fields'][1].update(value=' BR'),
    lambda i: i['fields'][1].update(value=''),
    lambda i: i['fields'][1].update(value=['BR']),
    lambda i: i.update(email_normalization={'contract': EMAIL_CONTRACT}),
])
def test_invalid_contract_rolls_back_all_items(writable, store, mutate):
    client, _, _, _ = writable
    item = postal(); mutate(item); before = counts(store)
    assert send(client, payload(items=[postal(key='valid-first'), item])).status_code == 422
    assert counts(store) == before


@pytest.mark.parametrize('collection', ['people', 'companies'])
def test_http_multiple_items_original_dates_flags_and_replay(writable, store, collection):
    client, _, _, user = writable
    first = postal(); first['fields'][0].update(source_updated_at='2026-02-01T01:00:00+01:00',
        flags={'residence_confirmed': {'value': True}, 'valid': {'value': None}})
    first['fields'].append({'path': 'street', 'value': 'Rua Fictícia', 'observed_at': '2026-01-01T00:00:00Z'})
    body = payload(items=[first, postal('SW1A 1AA', key='postal-2', country='GB'), postal('12-ABC', key='review')])
    before = counts(store); key = uuid4().hex
    response = send(client, body, key, collection); assert response.status_code == 200, response.text
    result = response.json(); owner = result['id']; rows = store.page_history(owner)['items']
    assert result['observations_created'] == 9 and response.headers['cache-control'] == 'no-store'
    assert counts(store)['outbox'] == before['outbox'] + 1
    assert rows[0]['input_value'] == '00123-456'
    assert rows[0]['normalized_value'] == '00123456'
    assert rows[0]['source_path'] == '/items/0/fields/0/value'
    assert rows[0]['source_updated_at'] == '2026-02-01T00:00:00+00:00'
    assert rows[0]['metadata']['postal_input_dates']['source_updated_at'].endswith('+01:00')
    assert all(r['actor_id'] == user['id'] and r['source_id'] == 'manual' for r in rows)
    assert rows[1]['pending_reason'] == rows[2]['pending_reason'] == 'value_mismatch'
    assert rows[1]['normalized_value'] is True and rows[2]['normalized_value'] is None
    assert rows[4]['normalized_value'] == 'Rua Fictícia' and rows[4]['observed_at'].startswith('2026-01-01')
    assert rows[-2]['status'] == 'pending' and rows[-2]['input_value'] == '12-ABC'
    assert len(store.get_entity(owner)['items']) == 3
    assert counts(store)['identity_keys'] == before['identity_keys']+1
    after = counts(store)
    assert send(client, body, key, collection).json() == {**result, 'replayed': True}
    assert counts(store) == after
    conflict = deepcopy(body); conflict['items'][0]['fields'][0]['value'] = '00999-999'
    assert send(client, conflict, key, collection).status_code == 409 and counts(store) == after
    other = deepcopy(body); other['source_record_id'] = uuid4().hex
    assert send(client, other, collection=collection).json()['id'] != owner


@pytest.mark.parametrize('value', [True, False, None])
def test_exact_binding_and_literal_reversal_do_not_move_flags(writable, store, value):
    client, _, _, _ = writable
    item = postal(); item['fields'][0]['flags'] = {'residence_confirmed': {'value': value,
        'confirmed_value': '00123456', 'checked_at': '2026-03-01T00:00:00Z'}}
    owner = send(client, payload(items=[item])).json()['id']
    current = store.page_fields(owner)['items']; field = next(r for r in current if r['dimension'] == 'value' and r['field_path'] == 'postal_code')
    flag = next(r for r in current if r['dimension'] == 'flag:residence_confirmed')
    assert flag['applicable'] and flag['value'] is value
    url = f"/api/v1/canonical/people/{owner}/items/{field['item_id']}/value"
    patch = {'source_id': 'manual', 'expected_version': 1, 'field_path': 'postal_code', 'value': field['input_value'],
             'observed_at': '2026-04-01T00:00:00Z', 'reason': 'Revisão sintética'}
    assert client.patch(url, json=patch, headers={'Idempotency-Key': uuid4().hex}).status_code == 200
    current = store.page_fields(owner)['items']; after = next(r for r in current if r['dimension'] == 'flag:residence_confirmed')
    assert after['observation_id'] == flag['observation_id'] and not after['applicable']
    history = store.page_history(owner)['items']
    assert len(history) == 4 and history[0]['metadata']['postal_normalization']['country'] == 'BR'
    assert history[-1]['normalized_value'] == '00123-456'
    assert history[-1]['metadata']['normalization'] == 'preserved-no-inference'


def test_late_future_absent_and_pending_do_not_overwrite_current(writable, store):
    client, _, _, _ = writable
    item = postal(); item['fields'].append({'path': 'street', 'value': 'Rua Fictícia'})
    body = payload(items=[item]); owner = send(client, body).json()['id']
    before = {r['field_path']: r['observation_id'] for r in store.page_fields(owner)['items']}
    for version, value, date in [(1, '00999-001', '2025-01-01T00:00:00Z'),
                                  (2, '00999-002', '2099-01-01T00:00:00Z'),
                                  (3, '00999-003', None),
                                  (4, 'BAD-CODE', '2026-04-01T00:00:00Z')]:
        changed = deepcopy(body); changed['expected_version'] = version
        item = postal(value); item['fields'][0]['observed_at'] = date; changed['items'] = [item]
        response = send(client, changed); assert response.status_code == 200, response.text
    after = {r['field_path']: r['observation_id'] for r in store.page_fields(owner)['items']}
    assert before['street'] == after['street'] and before['postal_code'] == after['postal_code']
    assert store.page_history(owner)['items'][-2]['pending_reason'] == 'pending_against_resolved'
    assert store.get_entity(owner)['version'] == 5


def test_mixed_phone_postal_contracts_preserve_independent_audit():
    phone = {'kind': 'phone', 'key': 'phone', 'phone_normalization': {'contract': PHONE_CONTRACT},
             'fields': [{'path': 'number', 'value': '+551188765432'}]}
    email = {'kind': 'email', 'key': 'email', 'email_normalization': {'contract': EMAIL_CONTRACT},
             'fields': [{'path': 'email', 'value': 'Case@EXAMPLE.INVALID'}]}
    result = prepare_enrichment(payload(items=[phone, postal(), email]), 'person')
    assert result['adapter_version'] == POSTAL_CONTRACT
    assert result['facts'][0]['phone_contract'] == PHONE_CONTRACT
    assert result['facts'][1]['postal_contract'] == POSTAL_CONTRACT
    assert result['facts'][0]['normalized_value'] == '+5511988765432'
    assert result['facts'][3]['email_contract'] == EMAIL_CONTRACT
    assert result['facts'][3]['normalized_value'] == 'Case@example.invalid'


def test_absent_country_never_reuses_stored_country_or_updates_other_components(writable, store):
    client, _, _, _ = writable
    item = postal()
    item['fields'][1].update(observed_at='2026-01-01T00:00:00Z', source_updated_at='2026-01-01T01:00:00+01:00')
    item['fields'] += [{'path': 'street', 'value': 'Rua Sintética'}, {'path': 'number', 'value': 0},
                       {'path': 'city', 'value': None}, {'path': 'extra', 'value': False}]
    body = payload(items=[item]); owner = send(client, body).json()['id']
    original = store.page_fields(owner)['items']
    context = store.page_history(owner)['items'][0]['metadata']['context_fields']['country']
    assert context == {key: item['fields'][1][key] for key in ('value', 'observed_at', 'source_updated_at')}
    body.update(expected_version=1, items=[postal('00999-999', country=None)])
    body['items'][0]['fields'][0]['observed_at'] = '2026-04-01T00:00:00Z'
    receipt = send(client, body).json()
    assert receipt['observations_created'] == 1
    assert store.page_fields(owner)['items'] == original
    last = store.page_history(owner)['items'][-1]
    assert last['normalized_value'] == '00999-999' and last['status'] == 'pending'
    assert last['metadata']['context_fields'] == {} and last['metadata']['postal_normalization']['country'] is None


def test_permission_source_revocation_and_replay(writable, store):
    client, app, _, user = writable
    body = payload(items=[postal()]); before = counts(store)
    token, _ = key_for(app, user, scopes=['enrich'], sources=['other'])
    assert send(client, body, headers={'X-API-Key': token}).status_code == 403
    token, key = key_for(app, user, scopes=['enrich'], sources=['manual'])
    body['items'][0]['fields'][0]['flags'] = {'residence_confirmed': {'value': True}}
    assert send(client, body, headers={'X-API-Key': token}).status_code == 403
    assert counts(store) == before
    body['items'][0]['fields'][0].pop('flags'); request_key = uuid4().hex
    response = send(client, body, request_key, headers={'X-API-Key': token}); assert response.status_code == 200
    rows = store.page_history(response.json()['id'])['items']
    assert len(rows) == 2 and rows[0]['metadata']['api_key_id'] == key['public_id']
    with app.state.store.transaction() as c:
        key['active'] = False; app.state.store.put(c, 'api_key', key)
    after = counts(store)
    assert send(client, body, request_key, headers={'X-API-Key': token}).status_code == 401
    assert counts(store) == after


def test_concurrent_normalization_atomic_rollback_and_replay(writable, store, monkeypatch):
    client, _, _, user = writable
    prepared = prepare_enrichment(payload(items=[postal()]), 'person'); key = uuid4().hex
    def write(request_key):
        try:
            return store.apply_enrichment(prepared, actor_id=user['id'], request_key=request_key, expected_version=0)
        except VersionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [key, key]))
    assert sorted(r['replayed'] for r in results) == [False, True]
    prepared = prepare_enrichment(payload(items=[postal()]), 'person')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [uuid4().hex, uuid4().hex]))
    assert results.count('conflict') == 1
    real = store._observe
    def fail(*args, **kwargs):
        real(*args, **kwargs); raise CanonicalError('Synthetic rollback after postal observation')
    monkeypatch.setattr(store, '_observe', fail); before = counts(store)
    assert send(client, payload(items=[postal()])).status_code == 422
    assert counts(store) == before
