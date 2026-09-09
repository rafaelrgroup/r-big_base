"""Explicit deployed search binding, using fictional files and HTTP only."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json

import httpx
import pytest

from bigbase import deployment_search as module
from bigbase.canonical_search import PROJECTION_VERSION, index_definition, ProjectionError
from bigbase.canonical_search_reader import InvalidSearchCursor, SearchReadError
from test_deployment_runtime import identity
import test_canonical_search_reader as fixtures


def payload(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(fixtures, 'INDEX', 'bigbase-canonical-fixture-000001')
    monkeypatch.setattr(fixtures, 'ALIAS', 'bigbase-canonical-fixture-read')
    wanted = identity()
    config = {'version': 1, 'enabled': True, 'canonical': asdict(wanted),
              'elasticsearch': {'url': 'http://127.0.0.1:19200', 'cluster_uuid': 'synthetic-cluster',
                  'index_uuid': 'synthetic-index', 'index': fixtures.INDEX, 'read_alias': fixtures.ALIAS,
                  'projection_version': PROJECTION_VERSION,
                  'mapping_sha256': module.sha(payload(index_definition()['mappings'])),
                  'api_key_file': '/synthetic/read-key.json'},
              'cursor_key_file': '/synthetic/cursor.key',
              'activation_receipt_file': '/synthetic/receipt.json', 'max_response_bytes': 2*1024*1024}
    receipt = {'version': 1, 'status': 'verified', 'scope': 'pilot_10000',
               'completed_at': datetime.now(timezone.utc).isoformat(),
               'config_sha256': module.sha(payload(config)),
               'canonical_deployment_id': wanted.deployment_id,
               'projection_binding_sha256': module.projection_binding(config),
               'verification_sha256': 'a'*64, 'source_records_verified': 10000,
               'entities_expected': 5, 'entities_verified': 5, 'indexed_entities': 5,
               'missing_entities': 0, 'extra_entities': 0, 'mismatched_entities': 0,
               'unpreserved_fields': 0, 'omissions_reviewed': True}
    files = {'/synthetic/config.json': payload(config), '/synthetic/read-key.json': payload({'api_key': 'synthetic-key-only'}),
             '/synthetic/cursor.key': fixtures.KEY, '/synthetic/receipt.json': payload(receipt)}
    monkeypatch.setattr(module, 'root_bytes', lambda path, **kw: files[str(path)])
    fake = fixtures.ElasticFake()
    clients = []
    def factory(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert kwargs['headers'] == {'Authorization': 'ApiKey synthetic-key-only'}
        client = httpx.Client(transport=httpx.MockTransport(fake.transport), **kwargs)
        clients.append(client)
        return client
    def load():
        return module.load_search('/synthetic/config.json', wanted, client_factory=factory)
    yield wanted, config, receipt, files, fake, clients, load
    for client in clients:
        client.close()


def test_read_only_activation_paging_restart_and_explicit_client_shutdown(setup):
    _, _, _, _, fake, clients, load = setup
    first = load()
    assert first.available()
    page = fixtures.search(first)
    second = load()
    page2 = fixtures.search(second, cursor=page['next_cursor'])
    assert page2['items'][0]['id'] == fixtures.owner(3)
    assert len([r for r in fake.requests if r[1].endswith('/_pit') and r[0] == 'POST']) == 1
    assert not any(method in {'PUT', 'PATCH'} for method, _, _ in fake.requests)
    second.close(page2['release_cursor'], principal_id='synthetic-user', authorization=fixtures.AUTH)
    assert not clients[1].is_closed
    second.shutdown()
    assert clients[1].is_closed and not second.available()


@pytest.mark.parametrize('name', ['/synthetic/config.json', '/synthetic/receipt.json',
                                  '/synthetic/cursor.key', '/synthetic/read-key.json'])
def test_runtime_revocation_disables_next_read_without_network(setup, name):
    _, _, _, files, fake, _, load = setup
    reader = load()
    before = len(fake.requests)
    files[name] += b' '
    assert not reader.available()
    with pytest.raises(SearchReadError, match='ACTIVATION_UNAVAILABLE'):
        fixtures.search(reader)
    assert len(fake.requests) == before


@pytest.mark.parametrize('change', [
    {'enabled': 'yes'}, {'max_response_bytes': 3*1024*1024},
    {'cursor_key_file': 'relative/key'}, {'version': True}, {'unexpected': True},
])
def test_invalid_configuration_is_rejected_before_network(setup, change):
    _, config, _, files, _, clients, load = setup
    files['/synthetic/config.json'] = payload({**config, **change})
    with pytest.raises(ValueError): load()
    assert not clients


@pytest.mark.parametrize('change', [
    {'url': 'http://127.0.0.1:9200'}, {'url': 'https://unapproved.invalid'},
    {'index': 'pessoas'}, {'projection_version': 'wrong'}, {'read_alias': 'pessoas_serasa'},
])
def test_reader_rejects_source_indices_and_other_destinations(setup, change):
    _, config, _, files, _, clients, load = setup
    config = {**config, 'elasticsearch': {**config['elasticsearch'], **change}}
    files['/synthetic/config.json'] = payload(config)
    with pytest.raises(ValueError): load()
    assert not clients


@pytest.mark.parametrize('change', [
    {'missing_entities': 1}, {'mismatched_entities': 1}, {'omissions_reviewed': False},
    {'entities_verified': 4}, {'source_records_verified': 9999}, {'unpreserved_fields': 1},
    {'canonical_deployment_id': 'other'}, {'config_sha256': 'b'*64},
])
def test_incomplete_or_misbound_projection_is_not_activated(setup, change):
    _, _, receipt, files, _, clients, load = setup
    files['/synthetic/receipt.json'] = payload({**receipt, **change})
    with pytest.raises(ValueError): load()
    assert not clients


def test_disabled_configuration_has_no_client_or_network_effect(setup):
    _, config, _, files, fake, clients, load = setup
    config['enabled'] = False
    files['/synthetic/config.json'] = payload(config)
    assert load() is None and not clients and not fake.requests


def test_cluster_failure_closes_client_and_does_not_open_pit(setup, monkeypatch):
    _, _, _, _, fake, clients, load = setup
    original = fake.transport
    monkeypatch.setattr(fake, 'transport', lambda request: httpx.Response(200, json={'cluster_uuid': 'wrong'})
                        if request.url.path == '/' else original(request))
    with pytest.raises(ProjectionError): load()
    assert len(clients) == 1 and clients[0].is_closed
