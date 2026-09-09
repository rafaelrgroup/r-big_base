"""Synthetic definition-bound search; no live Elasticsearch or personal records."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json

import pytest

from bigbase.canonical_search import (
    ProjectionError, build_projection, build_query, custom_selector, index_definition,
)
from bigbase.canonical_store import digest
from test_canonical_search import entity, item, field, flag, flat_fields

DEPLOYMENT = '760baef1-d347-4e9c-a4c6-d4d52b992d0e'


def metadata(kind='text', field_id='synthetic', version=1):
    definition = {'id': field_id, 'version': version, 'type': kind, 'name': 'Synthetic',
                  'active': True, 'multiple': True, 'scope': 'both', 'options': [],
                  'search_state': 'pending'}
    return {'field_id': field_id, 'field_definition_version': version,
            'field_definition': definition, 'field_definition_sha256': digest(definition),
            'custom_field': {'contract': 'canonical-postgresql-field-2026-09-09.1',
                             'field_id': field_id, 'version': version},
            'catalog_deployment_id': DEPLOYMENT, 'classification_state': 'defined',
            'canonical_search_state': 'pending'}


def criterion(meta, value, **extra):
    return {'kind': 'custom', 'field': 'value', 'custom': custom_selector(meta), 'value': value, **extra}


def projected(meta, value, **extras):
    return build_projection(entity([item('custom', [field('value', value, metadata=meta, **extras)])]))


@pytest.mark.parametrize('kind,value,token', [
    ('text', 'Árvore', ('value_exact', 'Árvore')),
    ('integer', 0, ('number_exact', '0e0')),
    ('integer', 10**50 + 1, ('number_exact', str(10**50 + 1) + 'e0')),
    ('boolean', False, ('value_boolean', False)),
    ('boolean', None, ('value_type', 'null')),
    ('decimal', '12345678901234567890.123456789', ('number_exact', '12345678901234567890123456789e-9')),
    ('date', '2024-02-29', ('value_date', '2024-02-29')),
    ('enum', 'alternative', ('value_exact', 'alternative')),
    ('url', 'https://example.invalid', ('value_exact', 'https://example.invalid')),
])
def test_typed_projection_query_preserve_input(kind, value, token):
    meta = metadata(kind)
    before = deepcopy(meta)
    with localcontext() as ctx:
        ctx.prec = 2
        document = projected(meta, value)
        query = build_query(criterion(meta, value))
    row = flat_fields(document)[0]
    assert row[token[0]] == token[1]
    assert row['custom_key'] in json.dumps(query)
    assert json.loads(row['custom_definition_json']) == custom_selector(meta)
    assert meta == before and meta['canonical_search_state'] == 'pending'
    assert not any(document['omitted'].values())


def test_decimal_numerical_equivalence_with_original_text_preserved():
    meta = metadata('decimal')
    values = ['1.000', 1, Decimal('1.00')]
    assert all(build_query(criterion(meta, value)) == build_query(criterion(meta, 1)) for value in values)
    assert [flat_fields(projected(meta, v))[0]['number_exact'] for v in values] == ['1e0'] * 3


@pytest.mark.parametrize('kind,value', [('integer', True), ('integer', '1'), ('boolean', 0),
    ('text', False), ('decimal', 'NaN'), ('decimal', float('inf')), ('date', '2023-02-29'),
    ('date', '2024-01-01T00:00:00Z'), ('date', 0)])
def test_type_mismatch_fails_projection_and_query(kind, value):
    meta = metadata(kind)
    with pytest.raises(ProjectionError): projected(meta, value)
    with pytest.raises(ProjectionError): build_query(criterion(meta, value))


@pytest.mark.parametrize('change', [
    {'field_id': 'other'}, {'field_definition_version': 2}, {'field_definition_sha256': '0'*64},
    {'custom_field': {}}, {'catalog_deployment_id': 'invalid'},
])
def test_damaged_bindings_fail_whole_projection(change):
    meta = metadata(); meta.update(change)
    with pytest.raises(ProjectionError): projected(meta, 'synthetic')


def test_unknown_and_reference_remain_explicitly_unindexed():
    assert projected({}, False)['omitted']['unbound_custom_fields'] == 1
    meta = metadata('reference')
    value = {'entity_type': 'person', 'id': DEPLOYMENT}
    doc = projected(meta, value)
    assert doc['omitted']['unsupported_values'] == 1 and not flat_fields(doc)
    with pytest.raises(ProjectionError, match='UNSUPPORTED_CUSTOM_REFERENCE_QUERY'):
        build_query(criterion(meta, value))


@pytest.mark.parametrize('key,value', [('version', 2), ('sha256', 'a'*64), ('field_id', 'other'),
    ('deployment_id', '760baef1-d347-4e9c-a4c6-d4d52b992d0f'), ('type', 'url')])
def test_each_definition_dimension_is_correlated_with_value(key, value):
    meta = metadata(); base = criterion(meta, 'synthetic')
    other = deepcopy(base); other['custom'][key] = value
    assert build_query(base) != build_query(other)
    nested = build_query(base)['query']['nested']['query']['bool']['filter'][1]['nested']
    assert nested['path'] == 'items.fields'
    clauses = nested['query']['bool']['filter']
    assert {'term': {'items.fields.value_exact': 'synthetic'}} in clauses
    assert any('items.fields.custom_key' in c.get('term', {}) for c in clauses)


@pytest.mark.parametrize('selector', [None, {}, {'field_id': 'x'}, [], 'x'])
def test_query_requires_explicit_selector(selector):
    with pytest.raises(ProjectionError, match='INVALID_CUSTOM_SELECTOR'):
        build_query({'kind': 'custom', 'field': 'value', 'value': 'x', 'custom': selector})


def test_date_range_and_unsupported_number_range():
    meta = metadata('date')
    query = build_query(criterion(meta, {'gte': '2024-01-01', 'lte': '2024-12-31'}, op='range'))
    assert 'items.fields.value_date' in json.dumps(query)
    with pytest.raises(ProjectionError, match='UNSUPPORTED_RANGE'):
        build_query(criterion(metadata('decimal'), {'gte': 0}, op='range'))
    with pytest.raises(ProjectionError):
        build_query(criterion(meta, {'gte': '2024-01-01T00:00:00Z'}, op='range'))


def test_nested_flags_sources_and_pending_are_not_transferred():
    meta = metadata('boolean')
    entries = [item('custom', [field('value', False, metadata=meta)],
                    [flag('value', 'valid', False)]),
               item('custom', [field('value', False, metadata=meta, status='pending')],
                    [flag('value', 'valid', True)])]
    rows = flat_fields(build_projection(entity(entries)))
    assert {r['flags']['valid']['state'] for r in rows} == {'false', 'unknown'}
    query = build_query(criterion(meta, False, flags={'valid': False}, source_id='synthetic'), include_invalid=True)
    assert 'items.fields.source_key' in json.dumps(query)
    assert 'items.fields.flags.valid.state' in json.dumps(query)
    assert 'items.fields.resolved' in json.dumps(query)


def test_dynamic_mapping_does_not_grow_with_definition_names():
    mapping = index_definition()['mappings']['properties']['items']['properties']['fields']
    assert mapping['dynamic'] == 'strict'
    assert mapping['properties']['custom_key'] == {'type': 'keyword'}
    assert mapping['properties']['custom_definition_json']['index'] is False
    assert 'synthetic' not in json.dumps(mapping)


def test_unsafe_values_counted_and_definition_versions_preserved():
    old = metadata('text', version=1); new = metadata('text', version=2)
    original = entity([item('custom', [field('value', 'x\x00y', metadata=old)]),
                       item('custom', [field('value', 'current', metadata=new)])])
    before = deepcopy(original)
    doc = build_projection(original)
    assert original == before and doc['omitted']['unsafe_text'] == 1
    assert json.loads(flat_fields(doc)[0]['custom_definition_json'])['version'] == 2


def test_decimal_exponent_outside_parser_range_has_sanitized_error():
    value = '1e' + '9' * 100
    with pytest.raises(ProjectionError, match='^CUSTOM_VALUE_TYPE_MISMATCH$'):
        build_query(criterion(metadata('decimal'), value))


def test_text_operators_remain_bounded_and_custom_not_allowed_on_other_kind():
    meta = metadata('text')
    for op in ['ieq', 'prefix', 'contains', 'match']:
        assert build_query(criterion(meta, 'synthetic', op=op))
    with pytest.raises(ProjectionError, match='SHORT_PREFIX_REQUIRES_SELECTIVE_FILTER'):
        build_query(criterion(meta, 'A', op='prefix'))
    with pytest.raises(ProjectionError, match='UNSUPPORTED_CUSTOM_OPERATOR'):
        build_query(criterion(metadata('decimal'), '1.000', op='contains'))
    query = criterion(meta, 'synthetic'); query.update(kind='identity', field='name')
    with pytest.raises(ProjectionError, match='INVALID_CUSTOM_SELECTOR'):
        build_query(query)


def test_legacy_definition_binding_is_separate_from_postgres():
    meta = metadata()
    pg_key = flat_fields(projected(meta, 'synthetic'))[0]['custom_key']
    meta['custom_field']['contract'] = 'canonical-custom-field-2026-09-09.1'
    meta.pop('catalog_deployment_id')
    local = flat_fields(projected(meta, 'synthetic'))[0]
    assert local['custom_key'] != pg_key
    assert custom_selector(meta)['deployment_id'] is None


@pytest.mark.parametrize('meta', [None, [], 'invalid', {'field_definition': None},
    {'custom_field': {}}, {'field_definition_sha256': '0' * 64},
    {**metadata(), 'field_definition_version': True}])
def test_incomplete_or_malformed_binding_is_not_an_unclassified_value(meta):
    with pytest.raises(ProjectionError, match='^INVALID_CUSTOM_DEFINITION$'):
        projected(meta, 'synthetic')


def test_broken_definition_never_publishes_or_acknowledges_outbox():
    from bigbase.canonical_search import OutboxConsumer
    from test_canonical_search import FakeStore, publisher
    meta = metadata()
    meta.pop('field_definition')
    store = FakeStore([1], entity([item('custom', [field('value', 'synthetic', metadata=meta)])]))
    def forbidden(request):
        pytest.fail('Damaged definition reached Elasticsearch')
    report = OutboxConsumer(store, publisher(forbidden)).run_once()
    assert report['failed'] == 1 and report['acknowledged'] == 0
    assert report['results'][0]['code'] == 'INVALID_CUSTOM_DEFINITION'
    assert not store.acknowledged


def test_custom_selector_is_bound_to_pit_cursor_and_rejected_before_network():
    from bigbase.canonical_search_reader import InvalidSearchCursor
    from test_canonical_search_reader import reader, search
    instance, fake, _ = reader()
    filters = criterion(metadata('boolean'), False)
    first = search(instance, criteria=filters)
    sent = next(body for method, path, body in fake.requests if path == '/_search')
    assert 'items.fields.custom_key' in json.dumps(sent['query'])
    count = len(fake.requests)
    changed = deepcopy(filters)
    changed['custom']['version'] += 1
    with pytest.raises(InvalidSearchCursor):
        search(instance, criteria=changed, cursor=first['next_cursor'])
    assert len(fake.requests) == count
    second = search(instance, criteria=filters, cursor=first['next_cursor'])
    assert not {hit['id'] for hit in first['items']} & {hit['id'] for hit in second['items']}
