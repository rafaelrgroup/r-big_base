"""Synthetic numbering examples; no calls, provider lookups or live data."""
from copy import deepcopy

import pytest

from bigbase.domain import ItemInput, add_observation, item_key, normalize, project, record_flags, NORMALIZER_VERSION
from bigbase.phone_rules import PHONE_RULES_VERSION, ROLLOUT_BY_DDD, ROLLOUT_GROUPS, original_phone_components


@pytest.mark.parametrize('ddd', sorted(ROLLOUT_BY_DDD))
@pytest.mark.parametrize('local', ['88765432', '98765432'])
def test_audited_legacy_prefixes_gain_one_digit_in_every_valid_ddd(ddd, local):
    original = {'number': '+55' + ddd + local}
    result, notes = normalize('phone', original)
    assert result['number'] == '+55' + ddd + '9' + local
    assert result['classification'] == 'mobile' and result['syntax_valid'] is True
    assert result['usage'] == 'unknown'
    audit = result['phone_normalization']
    assert audit['decision'] == 'historical_conversion' and audit['changed_digits'] is True
    assert audit['previous_number'] == original['number']
    assert audit['rollout_date'] == ROLLOUT_BY_DDD[ddd]
    assert len(audit['sources']) == 3 and all('anatel' in source for source in audit['sources'])
    assert original_phone_components(result) == original
    assert notes
    assert 'valid' not in result and 'is_whatsapp' not in result


def test_rollout_catalog_has_no_duplicate_areas_and_exact_historical_groups():
    assert len(ROLLOUT_BY_DDD) == 67
    assert sum(len(areas) for _, areas in ROLLOUT_GROUPS) == 67
    assert ROLLOUT_BY_DDD['11'] == '2012-07-29'
    assert ROLLOUT_BY_DDD['19'] == '2013-08-25'
    assert ROLLOUT_BY_DDD['28'] == '2013-10-27'
    assert ROLLOUT_BY_DDD['97'] == '2014-11-02'
    assert ROLLOUT_BY_DDD['89'] == '2015-05-31'
    assert ROLLOUT_BY_DDD['79'] == '2015-10-11'
    assert ROLLOUT_BY_DDD['65'] == '2016-05-29'
    assert ROLLOUT_BY_DDD['55'] == '2016-11-06'


@pytest.mark.parametrize('original', [
    {'number': '(11) 8876-5432', 'country': 'BR'},
    {'number': '88765432', 'country': 'BR', 'ddd': '11'},
    {'number': '551188765432', 'country': 'br'},
    {'number': '+55 (11) 8876-5432 ext. 007'},
])
def test_explicit_country_area_and_extensions_keep_original_components(original):
    untouched = deepcopy(original)
    result, _ = normalize('phone', original)
    assert result['number'] == '+5511988765432'
    assert result['ddd'] == '11' and result['country'] == 'BR'
    assert original_phone_components(result) == original == untouched
    if 'ext.' in original['number']:
        assert result['extension'] == '007'
    assert 'observed_at' not in result and 'source_id' not in result


@pytest.mark.parametrize(('value', 'reason'), [
    ({'number': '1188765432'}, 'country_required_for_historical_rule'),
    ({'number': '88765432', 'country': 'BR'}, 'missing_or_invalid_ddd'),
    ({'number': '88765432', 'ddd': '11'}, 'country_required_for_ddd'),
    ({'number': '+552388765432'}, 'missing_or_invalid_ddd'),
    ({'number': '+551168765432'}, 'historical_prefix_not_proven'),
    ({'number': '+551188765432', 'country': 'US'}, 'country_conflict'),
    ({'number': '+551188765432', 'ddd': '21'}, 'ddd_conflict'),
    ({'number': '+551188765432', 'ddd': 11}, 'invalid_ddd'),
    ({'number': '+551188765432', 'country': 'XX'}, 'unknown_country'),
    ({'number': '+551180012345'}, 'reserved_historical_range'),
    ({'number': '+551190012345'}, 'reserved_historical_range'),
    ({'number': '+1800FLOWERS'}, 'unsupported_characters'),
    ({'number': '+5511998765432 texto'}, 'unsupported_characters'),
    ({'number': '00551188765432'}, 'invalid_current_format'),
])
def test_ambiguities_conflicts_reserved_ranges_and_letters_are_not_rewritten(value, reason):
    result, notes = normalize('phone', value)
    assert result['number'] == value['number']
    assert result['canonical_number'] is None and result['syntax_valid'] is False
    assert result['phone_normalization']['reason'] == reason
    assert result['phone_normalization']['changed_digits'] is False and notes
    assert original_phone_components(result) == value


@pytest.mark.parametrize('value', [None, False, 0, 1188765432, [], {}])
def test_non_text_number_is_preserved_without_numeric_or_boolean_coercion(value):
    result, _ = normalize('phone', {'number': value})
    assert result['number'] == value and type(result['number']) is type(value)
    assert result['phone_normalization']['reason'] == 'non_text_or_empty_number'


@pytest.mark.parametrize(('raw', 'classification', 'canonical'), [
    ('+551133334444', 'fixed', '+551133334444'),
    ('+551155551234', 'fixed', '+551155551234'),
    ('+551157551234', 'fixed', '+551157551234'),
    ('+551178765432', 'other', '+551178765432'),
    ('+5511998765432', 'mobile', '+5511998765432'),
    ('08001234567', 'other', '+558001234567'),
    ('+442079460123', 'fixed', '+442079460123'),
])
def test_modern_fixed_specialized_special_and_international_numbers_do_not_gain_digits(raw, classification, canonical):
    result, _ = normalize('phone', {'number': raw})
    assert result['number'] == canonical and result['classification'] == classification
    assert result['phone_normalization']['changed_digits'] is False
    assert result['usage'] == 'unknown'
    if raw == '08001234567':
        assert 'ddd' not in result


def test_reprocessing_is_idempotent_and_review_can_recover_original():
    original = {'number': '(11) 8876-5432 ramal 001', 'country': 'BR'}
    first, _ = normalize('phone', original)
    second, _ = normalize('phone', first)
    assert second['number'] == first['number'] == '+5511988765432'
    assert second['extension'] == first['extension'] == '001'
    assert item_key('phone', first) == item_key('phone', second)
    assert first['phone_normalization']['changed_digits'] is True
    assert second['phone_normalization']['changed_digits'] is False
    recovered = original_phone_components(first)
    assert recovered == original
    recovered['number'] = 'edited separately'
    assert original_phone_components(first) == original
    assert PHONE_RULES_VERSION in NORMALIZER_VERSION


def test_extensions_conflict_without_losing_either_value():
    original = {'number': '+5511998765432 ext. 001', 'extension': '002'}
    result, _ = normalize('phone', original)
    assert result['number'] == original['number'] and result['extension'] == '002'
    assert result['phone_normalization']['extension_candidates'] == ['002', '001']
    assert result['phone_normalization']['reason'] == 'conflicting_extensions'


@pytest.mark.parametrize('flag_value', [True, False, None])
def test_legacy_confirmation_does_not_apply_to_transformed_number(flag_value):
    inp = ItemInput(kind='phone', value={'number': '+551188765432'}, flags={'is_whatsapp': flag_value, 'valid': True})
    normalized, _ = normalize(inp.kind, inp.value)
    entity = {'items': [], 'observations': []}
    item = {'id': 'synthetic-item', 'kind': 'phone', 'fields': {}, 'sources': []}
    entity['items'].append(item)
    for key, value in normalized.items():
        add_observation(entity, item, 'value.' + key, value, 'synthetic', None, 'actor', 'operation', '', '2026-01-01T00:00:00Z')
    record_flags(entity, item, inp, 'synthetic', None, 'actor', 'operation', '', '2026-01-01T00:00:00Z', confirmed_values=normalized)
    project(entity)
    assert item['flags'] == {}
    evidence = [row for row in entity['observations'] if row['path'].startswith('flag.')]
    assert all(row['applied'] is False and row['pending_reason'] == 'phone_number_transformed' for row in evidence)
    assert evidence[0]['value'] is flag_value
    # A new explicit confirmation of the current item can be applied separately.
    current = ItemInput(kind='phone', value={}, flags={'is_whatsapp': True})
    record_flags(entity, item, current, 'synthetic', '2026-02-01T00:00:00Z', 'actor', 'confirmation', '', '2026-02-01T00:00:00Z')
    project(entity)
    assert item['flags']['is_whatsapp'] is True
    assert entity['observations'][-1]['applied'] is True
