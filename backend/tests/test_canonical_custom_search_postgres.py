"""End-to-end synthetic HTTP/catalog/PG projection binding; no real ES writes."""
import json
from decimal import Decimal

import pytest

from bigbase.canonical_search import build_projection, build_query, custom_selector
from test_canonical_search import flat_fields
from test_canonical_catalog import catalog, create, pg_item, patch
from test_canonical_enrichment import writable, payload, send
from test_canonical_http import env
from test_canonical_store import store


@pytest.mark.parametrize('kind,value', [('integer', 0), ('boolean', False),
    ('decimal', '12345678901234567890.1234567890123456789'), ('date', '2024-02-29'), ('text', None)])
def test_persisted_catalog_observation_projects_without_rebinding(catalog, store, kind, value):
    definition = create(catalog, kind)
    response = send(catalog[0], payload(items=[pg_item(definition, value)]))
    assert response.status_code == 200
    owner = response.json()['id']
    before = store.get_entity(owner)
    document = build_projection(before)
    row = next(r for r in flat_fields(document) if r.get('custom_key'))
    persisted = next(f for i in before['items'] if i['kind'] == 'custom' for f in i['fields'])
    selector = custom_selector(persisted['metadata'])
    query = build_query({'kind': 'custom', 'field': 'value', 'value': value, 'custom': selector})
    assert row['custom_key'] in json.dumps(query)
    assert selector['deployment_id'] == str(store.deployment_info()['deployment_id'])
    assert persisted['value'] == value
    changed = patch(catalog, definition, {'name': 'New synthetic label', 'active': False})
    assert changed.status_code == 200
    assert build_projection(store.get_entity(owner)) == document
    assert store.get_entity(owner) == before
    if kind == 'decimal':
        numeric_query = build_query({'kind': 'custom', 'field': 'value', 'value': Decimal(value), 'custom': selector})
        assert numeric_query == query
