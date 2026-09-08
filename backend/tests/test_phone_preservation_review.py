"""Independent preservation review, using synthetic/library example numbers only."""
from copy import deepcopy

import phonenumbers
import pytest

from bigbase.domain import ItemInput, add_observation, item_key, normalize, project, record_flags
from bigbase.phone_rules import original_phone_components


@pytest.mark.parametrize(('actual_region', 'supplied_country'), [('BS', 'US'), ('CA', 'US'), ('GG', 'GB')])
def test_country_conflict_is_detected_even_with_shared_calling_code(actual_region, supplied_country):
    phone = phonenumbers.example_number(actual_region)
    raw = phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)
    original = {'number': raw, 'country': supplied_country}
    result, notes = normalize('phone', original)
    assert result['number'] == raw
    assert result['country'] == supplied_country
    assert result['phone_normalization']['decision'] == 'review'
    assert result['phone_normalization']['reason'] == 'country_conflict'
    assert result['canonical_number'] is None
    assert original_phone_components(result) == original
    assert notes


@pytest.mark.parametrize('original', [
    {'number': '+551188765432', 'extension': '007'},
    {'number': '(11) 9876-5432 ramal 001', 'country': 'BR'},
    {'number': '08001234567'},
    {'number': 'tel:+551133334444;ext=001'},
    {'number': '+5511998765432', 'extension': None},
    {'number': '+5511998765432', 'extension': ''},
    {'number': '1188765432'},
    {'number': '+5511998765432 ext. 001', 'extension': '002'},
])
def test_repeated_normalization_preserves_number_item_key_and_inputs(original):
    received = deepcopy(original)
    first, _ = normalize('phone', original)
    first_immutable = deepcopy(first)
    second, _ = normalize('phone', first)
    third, _ = normalize('phone', second)
    assert original == received
    assert first == first_immutable
    assert first['number'] == second['number'] == third['number']
    assert item_key('phone', first) == item_key('phone', second) == item_key('phone', third)
    assert original_phone_components(first) == received
    assert second['phone_normalization']['changed_digits'] is False
    assert third['phone_normalization']['changed_digits'] is False
    for value in (first, second, third):
        for generated_fact in ('observed_at', 'received_at', 'source_id', 'checked_at', 'valid', 'is_whatsapp'):
            assert generated_fact not in value
            assert generated_fact not in value['phone_normalization']


def _apply_values(entity, item, normalized, observed_at, operation):
    for key, value in normalized.items():
        add_observation(entity, item, 'value.' + key, value, 'synthetic', observed_at,
                        'reviewer', operation, '', '2026-03-01T00:00:00Z')


@pytest.mark.parametrize('legacy_observed_at', [None, '2026-01-01T00:00:00Z', '2026-03-01T00:00:00Z'])
def test_legacy_normalization_cannot_invalidate_a_current_confirmation(legacy_observed_at):
    item = {'id': 'synthetic-phone-review', 'kind': 'phone', 'fields': {}, 'sources': []}
    entity = {'items': [item], 'observations': []}
    current, _ = normalize('phone', {'number': '+5511988765432'})
    _apply_values(entity, item, current, '2026-02-01T00:00:00Z', 'current-value')
    inp = ItemInput(kind='phone', value={}, flags={'is_whatsapp': True, 'valid': True})
    record_flags(entity, item, inp, 'synthetic', '2026-02-01T00:00:00Z', 'reviewer',
                 'current-confirmation', '', '2026-03-01T00:00:00Z')
    preserved = {key: deepcopy(value) for key, value in item['fields'].items() if key.startswith('flag.')}

    legacy_input = ItemInput(kind='phone', value={'number': '+551188765432'},
                            flags={'is_whatsapp': False, 'valid': False})
    legacy, _ = normalize('phone', legacy_input.value)
    assert item_key('phone', current) == item_key('phone', legacy)
    _apply_values(entity, item, legacy, legacy_observed_at, 'legacy-value')
    record_flags(entity, item, legacy_input, 'synthetic', legacy_observed_at, 'reviewer',
                 'legacy-confirmation', '', '2026-03-01T00:00:00Z', confirmed_values=legacy)
    project(entity, at='2026-03-01T00:00:00Z')

    assert item['flags'] == {'is_whatsapp': True, 'valid': True}
    assert {key: value for key, value in item['fields'].items() if key.startswith('flag.')} == preserved
    evidence = [obs for obs in entity['observations'] if obs['operation_id'] == 'legacy-confirmation']
    assert len(evidence) == 2
    assert all(obs['value'] is False and obs['applied'] is False for obs in evidence)
    assert all(obs['pending_reason'] == 'phone_number_transformed' for obs in evidence)
    assert all(obs['observed_at'] == legacy_observed_at for obs in evidence)


def test_older_confirmation_of_the_same_canonical_number_keeps_newer_flag_and_date():
    item = {'id': 'synthetic-phone-review', 'kind': 'phone', 'fields': {}, 'sources': []}
    entity = {'items': [item], 'observations': []}
    value, _ = normalize('phone', {'number': '+5511998765432'})
    _apply_values(entity, item, value, '2026-02-01T00:00:00Z', 'current-value')
    for operation, observed_at, flag in [
        ('new-confirmation', '2026-02-01T00:00:00Z', True),
        ('late-old-confirmation', '2026-01-01T00:00:00Z', False),
    ]:
        inp = ItemInput(kind='phone', value={}, flags={'is_whatsapp': flag})
        record_flags(entity, item, inp, 'synthetic', observed_at, 'reviewer', operation,
                     '', '2026-03-01T00:00:00Z', confirmed_values=value)
    project(entity, at='2026-03-01T00:00:00Z')
    assert item['flags']['is_whatsapp'] is True
    assert item['flag_details']['is_whatsapp']['checked_at'] == '2026-02-01T00:00:00Z'
    assert entity['observations'][-1]['value'] is False
    assert entity['observations'][-1]['applied'] is False
    assert entity['observations'][-1]['observed_at'] == '2026-01-01T00:00:00Z'
