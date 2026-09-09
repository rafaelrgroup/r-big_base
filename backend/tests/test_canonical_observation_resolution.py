"""Semantics shared by relational writes and compact history reconstruction."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from bigbase.canonical_store import (
    CanonicalError, IdentityConflict, _prepare, digest, identifier,
    json_text, resolve_canonical_observation,
)
from test_canonical_store import atom, record


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
OWNER = UUID('00000000-0000-0000-0000-000000000010')
ITEM = UUID('00000000-0000-0000-0000-000000000020')


def resolve(fact, *, previous=None, value_state=None, flag=None, number=1):
    prepared = _prepare(record('synthetic', str(number), [fact]))
    return resolve_canonical_observation(
        owner=OWNER, item_id=ITEM, operation_id=prepared['operation_id'],
        source='synthetic', actor='test-actor', received=NOW + timedelta(seconds=number),
        fact=prepared['facts'][0], previous=previous, value_state=value_state,
        flag_name='is_whatsapp' if flag is not None else None, flag=flag,
        entity_version=number, operation_sequence=1, item_version=number,
    )


@pytest.mark.parametrize('value', [None, False, True, 0, '', [], {}, 'a\x00b\ud800'])
def test_exact_value_and_actor_are_in_history_and_current_state(value):
    fact = atom(value=value)
    original = deepcopy(fact)
    row, state = resolve(fact)
    assert row['input_json'] == row['normalized_json'] == state['value_json'] == json_text(value)
    assert row['source_id_json'] == '"synthetic"' and row['actor_id_json'] == '"test-actor"'
    assert row['received_at'] == NOW + timedelta(seconds=1)
    assert row['previous_observation_id'] is None and row['applied'] is True
    assert fact == original


@pytest.mark.parametrize('date,status,reason', [
    ('2026-01-01T00:00:00Z', 'unknown', 'older_observation'),
    (None, 'unknown', 'undated_against_dated'),
    ('2030-01-01T00:00:00Z', 'unknown', 'future_date'),
    ('2026-01-09T00:00:00Z', 'pending', 'pending_against_resolved'),
])
def test_rejected_value_still_retains_full_history(date, status, reason):
    _, prior = resolve(atom(value='current', source_updated_at='2026-01-08T00:00:00Z'))
    before = deepcopy(prior)
    row, state = resolve(atom(value='incoming', source_updated_at=date, status=status), previous=prior, number=2)
    assert state is None and row['applied'] is False and row['pending_reason'] == reason
    assert row['normalized_json'] == '"incoming"' and row['previous_value_json'] == '"current"'
    assert row['previous_observation_id'] == prior['observation_id'] and prior == before


@pytest.mark.parametrize('flag_value', [None, False, True])
def test_confirmation_keeps_tristate_and_is_bound_to_exact_value(flag_value):
    fact = atom(value='111', normalized_value='222')
    _, value = resolve(fact)
    flag = {'value': flag_value, 'observed_at': '2026-01-09T00:00:00Z'}
    row, state = resolve(fact, value_state=value, flag=flag)
    assert state is None and row['pending_reason'] == 'value_mismatch'
    assert row['normalized_json'] == json_text(flag_value)
    explicit = {**flag, 'confirmed_value_json': '"222"'}
    row, state = resolve(fact, value_state=value, flag=explicit)
    assert row['applied'] is True and state['value_json'] == json_text(flag_value)
    assert row['binding_hash'] == digest('222')


def test_confirmation_does_not_confirm_a_replaced_number():
    first = atom(value='old-number')
    _, value = resolve(first)
    _, confirmed = resolve(first, value_state=value, flag={'value': True})
    _, changed = resolve(atom(value='new-number'), previous=value, number=2)
    assert confirmed['binding_hash'] != digest('new-number')
    late, state = resolve(first, previous=confirmed, value_state=changed, flag={'value': False}, number=3)
    assert state is None and late['pending_reason'] == 'value_mismatch'
    assert late['previous_observation_id'] == confirmed['observation_id']


def test_missing_value_cannot_be_confirmed_and_bad_binding_is_rejected():
    row, state = resolve(atom(), flag={'value': True})
    assert state is None and row['pending_reason'] == 'value_mismatch'
    with pytest.raises(CanonicalError, match='JSON literal'):
        resolve(atom(), flag={'value': True, 'confirmed_value_json': 7})


def test_path_collision_never_reassigns_an_observation():
    _, value = resolve(atom())
    value['target_path_json'] = '"different"'
    with pytest.raises(IdentityConflict, match='path hash collision'):
        resolve(atom(), previous=value, number=2)
