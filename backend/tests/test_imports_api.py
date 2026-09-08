"""Synthetic API contracts for asynchronous enrichment and job isolation."""
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pyotp
import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.domain import now, uid
from bigbase.security import hashed


PASSWORD = 'synthetic-import-password-123'


def user_setup(app, username='import-admin', role='admin', permissions=None):
    with app.state.store.transaction() as tx:
        user = app.state.security.create_user(tx, username, PASSWORD, role, permissions)
        secret = app.state.security.secret(user)
    return user, secret


def authenticate(client, user, secret):
    challenge = client.post('/api/v1/auth/login', json={'username': user['username'], 'password': PASSWORD})
    assert challenge.status_code == 200, challenge.text
    result = client.post('/api/v1/auth/otp', json={'challenge': challenge.json()['challenge'], 'code': pyotp.TOTP(secret).now()})
    assert result.status_code == 200, result.text
    client.headers['X-CSRF-Token'] = result.json()['csrf']
    return result.json()


@pytest.fixture
def imports_env(tmp_path):
    app = create_app(tmp_path, testing=True)
    user, secret = user_setup(app)
    with patch.object(app.state.job_executor, 'submit') as submit:
        with TestClient(app) as client:
            yield client, app, user, secret, submit


def entries(count=1):
    return [{
        'source_id': 'manual', 'external_id': 'synthetic-import-' + str(index),
        'items': [{'kind': 'identity', 'value': {'name': 'ENTRADA_SINTETICA_PRIVADA_' + str(index)}}],
    } for index in range(count)]


def create_job(client, key='import-1', content=None, **extra):
    return client.post('/api/v1/imports', json={
        'name': 'Lote sintético', 'entries': entries() if content is None else content, **extra,
    }, headers={'Idempotency-Key': key})


def public_job_only(job):
    assert {'id', 'status', 'cursor', 'processed', 'total', 'progress_percent'} <= set(job)
    assert not {'entries', 'results', 'api_key_id'} & set(job)
    assert 'ENTRADA_SINTETICA_PRIVADA' not in json.dumps(job)


def api_key(app, user_id, *, scopes=('enrich',), sources=('manual',)):
    token = 'synthetic-import-key-' + uid()
    record = {
        'id': hashed(token), 'public_id': uid(), 'user_id': user_id, 'name': 'Chave sintética',
        'scopes': list(scopes), 'sources': list(sources), 'active': True, 'created_at': now(),
        'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    with app.state.store.transaction() as tx:
        app.state.store.put(tx, 'api_key', record)
    return token, record


def test_imports_require_completed_otp_and_csrf(imports_env):
    client, app, user, secret, _ = imports_env
    assert create_job(client).status_code == 401
    challenge = client.post('/api/v1/auth/login', json={'username': user['username'], 'password': PASSWORD}).json()
    assert challenge['enrollment_required'] is True
    assert create_job(client).status_code == 401
    assert client.get('/api/v1/imports').status_code == 401
    authenticated = client.post('/api/v1/auth/otp', json={'challenge': challenge['challenge'], 'code': pyotp.TOTP(secret).now()})
    assert authenticated.status_code == 200
    assert create_job(client).status_code == 403
    client.headers['X-CSRF-Token'] = authenticated.json()['csrf']
    job = create_job(client)
    assert job.status_code == 202, job.text
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/imports/' + job.json()['id'] + '/cancel').status_code == 403
    with app.state.store.transaction() as tx:
        assert app.state.store.get(tx, 'import_job', job.json()['id'])['status'] == 'pending'


def test_read_permission_does_not_allow_imports_or_job_visibility(imports_env):
    client, app, _, _, _ = imports_env
    user, secret = user_setup(app, 'import-reader', 'user', ['read'])
    authenticate(client, user, secret)
    assert create_job(client).status_code == 403
    assert client.get('/api/v1/imports').status_code == 403
    assert client.get('/api/v1/imports/unknown').status_code == 403


def test_import_idempotency_schedules_once_and_rejects_different_content(imports_env):
    client, app, user, secret, submit = imports_env
    authenticate(client, user, secret)
    body = {'name': 'Lote sintético', 'entries': entries()}
    assert client.post('/api/v1/imports', json=body).status_code == 422
    first = create_job(client)
    second = create_job(client)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    public_job_only(first.json())
    assert submit.call_count == 1
    changed = create_job(client, name='Conteúdo diferente')
    assert changed.status_code == 409
    with app.state.store.transaction() as tx:
        assert len(app.state.store.all(tx, 'import_job')) == 1


def test_import_progress_results_and_pagination_do_not_expose_entries(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    content = entries(3)
    content[1]['items'][0]['kind'] = 'unknown-kind'
    job = create_job(client, content=content).json()
    assert job['status'] == 'pending' and job['progress_percent'] == 0 and job['processed'] == 0
    app.state.imports.run(job['id'])
    response = client.get('/api/v1/imports/' + job['id'] + '?offset=0&limit=2')
    assert response.status_code == 200, response.text
    first_page = response.json()
    public_job_only(first_page['job'])
    assert first_page['job']['processed'] == 3 and first_page['job']['cursor'] == 3
    assert first_page['job']['progress_percent'] == 100
    assert first_page['job']['succeeded'] == 2 and first_page['job']['failed_count'] == 1
    assert first_page['offset'] == 0 and first_page['limit'] == 2 and first_page['complete'] is False
    assert first_page['total_results'] == 3 and len(first_page['results']) == 2
    assert [row['status'] for row in first_page['results']] == ['succeeded', 'failed']
    second_page = client.get('/api/v1/imports/' + job['id'] + '?offset=2&limit=2').json()
    assert second_page['complete'] is True and len(second_page['results']) == 1
    assert second_page['results'][0]['index'] == 2 and second_page['results'][0]['status'] == 'succeeded'
    assert 'ENTRADA_SINTETICA_PRIVADA' not in response.text
    assert 'ENTRADA_SINTETICA_PRIVADA' not in json.dumps(second_page)
    assert 'entries' not in response.text and 'api_key_id' not in response.text
    listing = client.get('/api/v1/imports').json()
    assert len(listing['items']) == 1
    public_job_only(listing['items'][0])
    for query in ('offset=-1', 'offset=1001', 'limit=0', 'limit=101', 'limit=abc'):
        assert client.get('/api/v1/imports/' + job['id'] + '?' + query).status_code == 422
    with app.state.store.transaction() as tx:
        assert len(app.state.store.all(tx, 'entity')) == 2
    assert client.post('/api/v1/imports/' + job['id'] + '/cancel').status_code == 409


def test_cancel_partial_import_preserves_checkpoint_and_original_entries(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    content = entries(2)
    job = create_job(client, content=content).json()
    result = {'index': 0, 'input_id': '1', 'status': 'succeeded', 'entity_id': 'synthetic-checkpoint-entity', 'entity_version': 1, 'processed_at': now()}
    with app.state.store.transaction() as tx:
        saved = app.state.store.get(tx, 'import_job', job['id'])
        saved.update(status='processing', cursor=1, processed=1, succeeded=1, progress_percent=50, results=[result])
        app.state.store.put(tx, 'import_job', saved)
    cancelled = client.post('/api/v1/imports/' + job['id'] + '/cancel')
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()['status'] == 'cancelled'
    assert cancelled.json()['cursor'] == 1 and cancelled.json()['processed'] == 1
    with app.state.store.transaction() as tx:
        after = app.state.store.get(tx, 'import_job', job['id'])
        assert after['entries'] == content and after['results'] == [result]
        assert after['cursor'] == 1
    app.state.imports.run(job['id'])
    with app.state.store.transaction() as tx:
        assert app.state.store.get(tx, 'import_job', job['id'])['cursor'] == 1
    assert client.post('/api/v1/imports/' + job['id'] + '/cancel').status_code == 409


def test_other_owner_cannot_read_cancel_or_list_import(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    job = create_job(client).json()
    second, second_secret = user_setup(app, 'another-import-owner', 'user', ['read', 'enrich'])
    authenticate(client, second, second_secret)
    assert client.get('/api/v1/imports').json()['items'] == []
    assert client.get('/api/v1/imports/' + job['id']).status_code == 404
    assert client.post('/api/v1/imports/' + job['id'] + '/cancel').status_code == 404


def test_enrich_only_api_key_can_track_its_job_without_read_permission(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    token, record = api_key(app, user['id'])
    client.headers['X-API-Key'] = token
    client.headers.pop('X-CSRF-Token')
    created = create_job(client)
    assert created.status_code == 202, created.text
    job = created.json()
    assert record['id'] not in created.text and token not in created.text
    assert client.get('/api/v1/imports/' + job['id']).status_code == 200
    assert [item['id'] for item in client.get('/api/v1/imports').json()['items']] == [job['id']]
    assert client.post('/api/v1/people/search', json={}).status_code == 403
    app.state.imports.run(job['id'])
    detail = client.get('/api/v1/imports/' + job['id']).json()
    assert detail['job']['processed'] == 1 and detail['results'][0]['status'] == 'succeeded'


def test_other_key_of_same_owner_cannot_read_job_or_replay_cached_response(imports_env):
    client, app, user, secret, _ = imports_env
    authenticated = authenticate(client, user, secret)
    first_token, _ = api_key(app, user['id'])
    second_token, _ = api_key(app, user['id'])
    client.headers['X-API-Key'] = first_token
    first = create_job(client).json()
    client.headers['X-API-Key'] = second_token
    assert client.get('/api/v1/imports').json()['items'] == []
    assert client.get('/api/v1/imports/' + first['id']).status_code == 404
    assert client.post('/api/v1/imports/' + first['id'] + '/cancel').status_code == 404
    replay = create_job(client)
    assert replay.status_code in (404, 409), replay.text
    assert first['id'] not in replay.text
    second = create_job(client, key='second-key-own-job')
    assert second.status_code == 202, second.text
    assert second.json()['id'] != first['id']
    client.headers.pop('X-API-Key')
    client.headers['X-CSRF-Token'] = authenticated['csrf']
    assert client.get('/api/v1/imports/' + first['id']).status_code == 200
    assert len(client.get('/api/v1/imports').json()['items']) == 2


@pytest.mark.parametrize('body', [
    {'name': 'Vazio', 'entries': []},
    {'name': 'Errado', 'entries': {}},
    {'name': 'Errado', 'entries': None},
    {'name': 'Errado'},
    {'name': 'Extra', 'entries': entries(), 'unexpected': False},
    {'name': 'Grande', 'entries': [{} for _ in range(1001)]},
])
def test_import_envelope_rejects_invalid_shape_and_excess_entries(imports_env, body):
    client, app, user, secret, submit = imports_env
    authenticate(client, user, secret)
    response = client.post('/api/v1/imports', json=body, headers={'Idempotency-Key': 'invalid-envelope'})
    assert response.status_code == 422, response.text
    assert submit.call_count == 0
    with app.state.store.transaction() as tx:
        assert app.state.store.all(tx, 'import_job') == []


def test_import_accepts_exactly_1000_entries_and_body_limit_is_enforced(imports_env):
    client, app, user, secret, submit = imports_env
    authenticate(client, user, secret)
    content = entries(1000)
    response = create_job(client, content=content)
    assert response.status_code == 202 and response.json()['total'] == 1000
    oversized = {'name': 'Grande', 'entries': entries()}
    oversized['entries'][0]['items'][0]['value']['name'] = 'x' * (2 * 1024 * 1024)
    response = client.post('/api/v1/imports', json=oversized, headers={'Idempotency-Key': 'oversized'})
    assert response.status_code == 413, response.text
    assert submit.call_count == 1
    with app.state.store.transaction() as tx:
        assert len(app.state.store.all(tx, 'import_job')) == 1


def test_user_job_quota_is_shared_with_exports_in_both_directions(imports_env):
    client, _, user, secret, _ = imports_env
    authenticate(client, user, secret)
    first = create_job(client)
    assert first.status_code == 202
    export = client.post('/api/v1/bulk-queries', json={'all_records': True}, headers={'Idempotency-Key': 'export-quota'})
    assert export.status_code == 202, export.text
    assert create_job(client, key='import-full').status_code == 429
    denied_export = client.post('/api/v1/bulk-queries', json={'all_records': True}, headers={'Idempotency-Key': 'export-full'})
    assert denied_export.status_code == 429 and denied_export.headers.get('Retry-After')
    client.post('/api/v1/imports/' + first.json()['id'] + '/cancel')
    assert create_job(client, key='import-after-cancel').status_code == 202


def test_global_job_quota_does_not_depend_on_requesting_owner(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    with app.state.store.transaction() as tx:
        for index in range(10):
            owner = 'synthetic-other-owner-' + str(index)
            job = app.state.imports.prepare(entries(), owner, 'Quota sintética')
            app.state.store.put(tx, 'import_job', job)
    denied = create_job(client)
    assert denied.status_code == 429 and denied.headers.get('Retry-After')


def test_pending_and_processing_imports_are_rescheduled_after_app_restart(tmp_path):
    first_app = create_app(tmp_path, testing=True)
    user, _ = user_setup(first_app)
    ids = {}
    with first_app.state.store.transaction() as tx:
        for status in ('pending', 'processing', 'cancelled', 'completed', 'failed'):
            job = first_app.state.imports.prepare(entries(), user['id'], 'Reinício sintético ' + status)
            job['status'] = status
            first_app.state.store.put(tx, 'import_job', job)
            ids[status] = job['id']
    first_app.state.job_executor.shutdown(wait=True)
    second_app = create_app(tmp_path, testing=True)
    with patch.object(second_app.state.job_executor, 'submit') as submit:
        with TestClient(second_app):
            scheduled = [call.args[1] for call in submit.call_args_list if call.args[0] == second_app.state.imports.run]
            assert sorted(scheduled) == sorted([ids['pending'], ids['processing']])


def test_resume_partial_import_processes_only_remaining_rows_with_original_key(imports_env):
    client, app, user, secret, submit = imports_env
    authenticate(client, user, secret)
    token, original_key = api_key(app, user['id'])
    client.headers['X-API-Key'] = token
    content = entries(2)
    created = create_job(client, content=content).json()
    original_put = app.state.store.put

    def stop_after_committed_first_entry(tx, kind, record):
        # Place the synthetic interruption exactly at the durable boundary after
        # a real enrichment, so the resume test can detect duplicated evidence.
        if kind == 'import_job' and record['id'] == created['id'] and record['processed'] == 1:
            record['status'] = 'cancelled'
        return original_put(tx, kind, record)

    with patch.object(app.state.store, 'put', side_effect=stop_after_committed_first_entry):
        app.state.imports.run(created['id'])
    with app.state.store.transaction() as tx:
        interrupted = app.state.store.get(tx, 'import_job', created['id'])
        assert interrupted['status'] == 'cancelled' and interrupted['cursor'] == 1
        first_entity = copy.deepcopy(app.state.store.get(tx, 'entity', interrupted['results'][0]['entity_id']))
        assert len(app.state.store.all(tx, 'entity')) == 1
    url = '/api/v1/imports/' + created['id'] + '/resume'
    resumed = client.post(url, json={}, headers={'Idempotency-Key': 'resume-once'})
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()['cursor'] == 1 and resumed.json()['status'] == 'pending'
    replay = client.post(url, json={}, headers={'Idempotency-Key': 'resume-once'})
    assert replay.status_code == 202 and replay.json() == resumed.json()
    assert submit.call_count == 2  # Initial scheduling plus exactly one resume.
    with app.state.store.transaction() as tx:
        queued = app.state.store.get(tx, 'import_job', created['id'])
        assert queued['api_key_id'] == original_key['id']
        assert queued['entries'] == content and queued['results'] == interrupted['results']
        assert queued['resume_history'][0]['previous_status'] == 'cancelled'
        assert queued['resume_history'][0]['processed'] == 1
    app.state.imports.run(created['id'])
    with app.state.store.transaction() as tx:
        final = app.state.store.get(tx, 'import_job', created['id'])
        assert final['status'] == 'completed' and final['processed'] == 2
        assert final['results'][0] == interrupted['results'][0]
        assert len(app.state.store.all(tx, 'entity')) == 2
        assert app.state.store.get(tx, 'entity', first_entity['id']) == first_entity
        assert final['api_key_id'] == original_key['id']


def test_resume_rejects_changed_entries_bad_checkpoints_and_unfinished_job_state(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    created = create_job(client).json()
    url = '/api/v1/imports/' + created['id'] + '/resume'
    assert client.post(url, json={}, headers={'Idempotency-Key': 'resume-pending'}).status_code == 409
    client.post('/api/v1/imports/' + created['id'] + '/cancel')
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={'entries': entries(2)}, headers={'Idempotency-Key': 'resume-modified'}).status_code == 422
    with app.state.store.transaction() as tx:
        stored = app.state.store.get(tx, 'import_job', created['id'])
        stored.update(status='failed', error={'code': 'checkpoint_inconsistent', 'message': 'Sintético'})
        app.state.store.put(tx, 'import_job', stored)
    assert client.post(url, json={}, headers={'Idempotency-Key': 'resume-bad-checkpoint'}).status_code == 409
    with app.state.store.transaction() as tx:
        assert app.state.store.get(tx, 'import_job', created['id'])['error']['code'] == 'checkpoint_inconsistent'


def test_resume_obeys_quotas_and_api_key_ownership_even_for_cached_response(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    token, _ = api_key(app, user['id'])
    other_token, _ = api_key(app, user['id'])
    client.headers['X-API-Key'] = token
    created = create_job(client).json()
    url = '/api/v1/imports/' + created['id'] + '/resume'
    client.post('/api/v1/imports/' + created['id'] + '/cancel')
    queued = [create_job(client, key='queued-' + str(index)).json() for index in range(2)]
    assert client.post(url, json={}, headers={'Idempotency-Key': 'resume-quota'}).status_code == 429
    client.post('/api/v1/imports/' + queued[0]['id'] + '/cancel')
    resumed = client.post(url, json={}, headers={'Idempotency-Key': 'resume-quota'})
    assert resumed.status_code == 202, resumed.text
    client.headers['X-API-Key'] = other_token
    denied = client.post(url, json={}, headers={'Idempotency-Key': 'resume-quota'})
    assert denied.status_code == 404 and created['id'] not in denied.text


def test_human_resume_preserves_old_error_and_cannot_bypass_revoked_original_key(imports_env):
    client, app, user, secret, _ = imports_env
    authenticate(client, user, secret)
    token, key = api_key(app, user['id'])
    client.headers['X-API-Key'] = token
    created = create_job(client).json()
    previous_error = {'code': 'import_processing_failed', 'message': 'Interrupção sintética', 'status_code': 500}
    with app.state.store.transaction() as tx:
        job = app.state.store.get(tx, 'import_job', created['id'])
        job.update(status='failed', error=previous_error)
        app.state.store.put(tx, 'import_job', job)
        key['active'] = False
        app.state.store.put(tx, 'api_key', key)
    client.headers.pop('X-API-Key')
    resumed = client.post('/api/v1/imports/' + created['id'] + '/resume', json={}, headers={'Idempotency-Key': 'human-resume'})
    assert resumed.status_code == 202, resumed.text
    app.state.imports.run(created['id'])
    with app.state.store.transaction() as tx:
        final = app.state.store.get(tx, 'import_job', created['id'])
        assert final['status'] == 'failed' and final['processed'] == 0
        assert final['api_key_id'] == key['id']
        assert final['error']['code'] == 'api_key_revoked'
        assert final['resume_history'][0]['previous_error'] == previous_error
        assert final['entries'] == entries() and app.state.store.all(tx, 'entity') == []
