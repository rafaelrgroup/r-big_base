"""HTTP contract with simulated catalog connection and internal proof provider.

No PostgreSQL/Elasticsearch integration claim; all definitions are synthetic.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import bigbase.canonical_catalog as module
from bigbase.canonical_store import digest, json_text
from test_canonical_search_availability import TARGET, CUT, proof
from test_canonical_custom_search import metadata

URL = '/api/v1/canonical/fields/synthetic/versions/1/search-availability'


@pytest.fixture
def endpoint(monkeypatch):
    meta = metadata('boolean')
    state = SimpleNamespace(meta=meta, environment='synthetic', calls=[], authorized=True,
                            row=None, context=None, error=None)
    state.row = {'definition_json': json_text(meta['field_definition']),
                 'definition_sha256': meta['field_definition_sha256']}
    receipt = proof(meta)
    now = datetime.now(timezone.utc)
    receipt.update(checked_at=(now-timedelta(seconds=1)).isoformat(),
                   expires_at=(now+timedelta(seconds=100)).isoformat())
    state.context = {'target': deepcopy(TARGET), 'coverage_cut_sha256': CUT, 'receipt': receipt}

    class Connection:
        def execute(self, sql, params):
            state.calls.append(('sql', params))
            assert 'field_catalog_versions' in sql and params == ('synthetic', 1)
            return self
        def fetchone(self):
            return state.row

    @contextmanager
    def connection():
        yield Connection()

    def provider(selector):
        state.calls.append(('provider', deepcopy(selector)))
        if state.error:
            raise state.error
        return deepcopy(state.context)

    def auth(c, request, permission):
        state.calls.append(('auth', permission))
        if not state.authorized:
            raise HTTPException(401, 'unauthorized')
        return {'id': 'synthetic-reader'}, None

    def verify():
        state.calls.append(('verify',))

    monkeypatch.setattr(module, 'verify_catalog', lambda c: {
        'environment': state.environment, 'deployment_id': TARGET['deployment_id']})
    reads = SimpleNamespace(repository=SimpleNamespace(connection=connection), verify=verify,
                            search_availability_provider=provider)
    app = FastAPI()
    module.install_canonical_catalog(app, reads, store=SimpleNamespace(transaction=connection),
                                     security=None, auth=auth)
    with TestClient(app) as client:
        yield client, reads, state


def test_ready_is_historical_no_store_and_not_execution_permission(endpoint):
    client, _, state = endpoint
    result = client.get(URL)
    assert result.status_code == 200, result.text
    assert result.headers['cache-control'] == 'no-store'
    data = result.json()
    assert data['availability']['selectable'] is True
    assert data['availability']['selector']['version'] == 1
    assert data['search_execution_enabled'] is False
    assert state.calls[:2] == [('auth', 'read'), ('verify',)]
    assert state.calls[-1][0] == 'provider'


def test_auth_before_catalog_and_provider(endpoint):
    client, _, state = endpoint
    state.authorized = False
    assert client.get(URL).status_code == 401
    assert state.calls == [('auth', 'read')]


@pytest.mark.parametrize('query', ['receipt=forged', 'target=forged', 'coverage_cut_sha256=forged', 'now=2099'])
def test_client_cannot_supply_internal_context(endpoint, query):
    client, _, state = endpoint
    assert client.get(URL+'?'+query).status_code == 422
    assert not any(c[0] in {'sql', 'provider'} for c in state.calls)


@pytest.mark.parametrize('version', ['0', '-1', '9223372036854775808', 'true'])
def test_invalid_version(endpoint, version):
    client, _, state = endpoint
    assert client.get(URL.replace('/1/', '/'+version+'/')).status_code == 422
    assert not any(c[0] in {'sql', 'provider'} for c in state.calls)


def test_missing_provider_default_disabled(endpoint):
    client, reads, state = endpoint
    del reads.search_availability_provider
    assert client.get(URL).json()['detail']['code'] == 'SEARCH_AVAILABILITY_DISABLED'
    assert not any(c[0] == 'sql' for c in state.calls)


@pytest.mark.parametrize('environment', ['staging', 'production'])
def test_deployed_database_refused(endpoint, environment):
    client, _, state = endpoint
    state.environment = environment
    assert client.get(URL).status_code == 503
    assert not any(c[0] in {'sql', 'provider'} for c in state.calls)


def test_unknown_version(endpoint):
    client, _, state = endpoint
    state.row = None
    assert client.get(URL).status_code == 404
    assert not any(c[0] == 'provider' for c in state.calls)


@pytest.mark.parametrize('change', ['hash', 'id', 'version'])
def test_damaged_history_never_reaches_provider(endpoint, change):
    client, _, state = endpoint
    if change == 'hash':
        state.row['definition_sha256'] = '0'*64
    else:
        definition = deepcopy(state.meta['field_definition'])
        definition[change] = 'other' if change == 'id' else 2
        state.row = {'definition_json': json_text(definition), 'definition_sha256': digest(definition)}
    assert client.get(URL).status_code == 503
    assert not any(c[0] == 'provider' for c in state.calls)


@pytest.mark.parametrize('case,reason', [('missing', 'PROOF_REQUIRED'),
    ('expired', 'AVAILABILITY_PROOF_EXPIRED'), ('index', 'AVAILABILITY_PROOF_CONTEXT_MISMATCH'),
    ('version', 'AVAILABILITY_PROOF_CONTEXT_MISMATCH'), ('incomplete', 'INCOMPLETE_SEARCH_COVERAGE')])
def test_unavailable_proof_is_never_selectable(endpoint, case, reason):
    client, _, state = endpoint
    receipt = state.context['receipt']
    if case == 'missing': state.context['receipt'] = None
    elif case == 'expired':
        now = datetime.now(timezone.utc)
        receipt.update(checked_at=(now-timedelta(seconds=120)).isoformat(),
                       expires_at=(now-timedelta(seconds=1)).isoformat())
    elif case == 'index': receipt['target']['index_uuid'] = 'other'
    elif case == 'version': receipt['selector']['version'] = 2
    else: receipt['counts']['failed'] = 1
    result = client.get(URL)
    assert result.status_code == 200, result.text
    availability = result.json()['availability']
    assert availability['selectable'] is False and availability['reason'] == reason


def test_provider_exception_sanitized(endpoint):
    client, _, state = endpoint
    state.error = RuntimeError('synthetic-secret-sentinel')
    result = client.get(URL)
    assert result.status_code == 503
    assert 'synthetic-secret-sentinel' not in result.text


@pytest.mark.parametrize('context', [None, {}, {'now': 'forged'}])
def test_malformed_internal_context(endpoint, context):
    client, _, state = endpoint
    state.context = context
    assert client.get(URL).status_code == 503


def test_each_request_reloads_proof(endpoint):
    client, _, state = endpoint
    assert client.get(URL).json()['availability']['selectable']
    state.context['receipt'] = None
    assert not client.get(URL).json()['availability']['selectable']
    assert sum(call[0] == 'provider' for call in state.calls) == 2


def test_application_authentication_and_key_revocation(tmp_path):
    from bigbase.api import create_app
    from test_canonical_http import key_for

    app = create_app(tmp_path, testing=True)
    with app.state.store.transaction() as c:
        user = app.state.security.create_user(c, 'availability-user', 'synthetic-password-123', 'user')
    enroll(app, user)
    token, key = key_for(app, user)
    with TestClient(app) as client:
        assert client.get(URL).status_code == 401
        headers = {'X-API-Key': token}
        result = client.get(URL, headers=headers)
        assert result.status_code == 503
        assert result.json()['detail']['code'] == 'CANONICAL_CATALOG_DISABLED'
        with app.state.store.transaction() as c:
            key['active'] = False
            app.state.store.put(c, 'api_key', key)
        assert client.get(URL, headers=headers).status_code == 401


def test_application_requires_read_scope(tmp_path):
    from bigbase.api import create_app
    from test_canonical_http import key_for

    app = create_app(tmp_path, testing=True)
    with app.state.store.transaction() as c:
        user = app.state.security.create_user(c, 'availability-user', 'synthetic-password-123', 'user')
    enroll(app, user)
    token, _ = key_for(app, user, scopes=['enrich'])
    with TestClient(app) as client:
        assert client.get(URL, headers={'X-API-Key': token}).status_code == 403


@pytest.mark.parametrize('value', [None, [], True, {'id': 'synthetic', 'version': True}])
def test_malformed_definition_is_unavailable(endpoint, value):
    client, _, state = endpoint
    state.row = {'definition_json': json_text(value), 'definition_sha256': digest(value)}
    assert client.get(URL).status_code == 503
    assert not any(c[0] == 'provider' for c in state.calls)


def enroll(app, user):
    import pyotp
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={
            'username': user['username'], 'password': 'synthetic-password-123'}).json()
        result = client.post('/api/v1/auth/otp', json={'challenge': login['challenge'],
            'code': pyotp.TOTP(app.state.security.secret(user)).now()})
        assert result.status_code == 200
